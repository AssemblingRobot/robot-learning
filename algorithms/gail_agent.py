from collections import OrderedDict

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.distributions
from torch.optim.lr_scheduler import StepLR

from .base_agent import BaseAgent
from .dataset import ReplayBuffer, RandomSampler
from .expert_dataset import ExpertDataset
from ..networks import Actor, Critic
from ..networks.discriminator import Discriminator
from ..utils.info_dict import Info
from ..utils.logger import logger
from ..utils.mpi import mpi_average
from ..utils.pytorch import (
    optimizer_cuda,
    count_parameters,
    compute_gradient_norm,
    compute_weight_norm,
    sync_networks,
    sync_grads,
    obs2tensor,
    to_tensor,
)


class GAILAgent(BaseAgent):
    def __init__(self, config, ob_space, ac_space):
        super().__init__(config, ob_space)

        self._ob_space = ob_space
        self._ac_space = ac_space

        # build up networks
        self._actor = Actor(config, ob_space, ac_space, config.tanh_policy)
        self._old_actor = Actor(config, ob_space, ac_space, config.tanh_policy)
        self._critic = Critic(config, ob_space)
        self._discriminator = Discriminator(config, ob_space, ac_space if not config.gail_no_action else None)
        self._discriminator_loss = nn.BCEWithLogitsLoss()
        self._network_cuda(config.device)

        # build optimizers
        self._actor_optim = optim.Adam(self._actor.parameters(), lr=config.actor_lr)
        self._critic_optim = optim.Adam(self._critic.parameters(), lr=config.critic_lr)
        self._discriminator_optim = optim.Adam(
            self._discriminator.parameters(), lr=config.discriminator_lr
        )

        self._actor_lr_scheduler = StepLR(
            self._actor_optim,
            step_size=self._config.max_global_step // self._config.rollout_length // 5,
            gamma=0.5,
        )
        self._critic_lr_scheduler = StepLR(
            self._critic_optim,
            step_size=self._config.max_global_step // self._config.rollout_length // 5,
            gamma=0.5,
        )
        self._discriminator_lr_scheduler = StepLR(
            self._discriminator_optim,
            step_size=self._config.max_global_step // self._config.rollout_length // 5,
            gamma=0.5,
        )

        # expert dataset
        self._dataset = ExpertDataset(config.demo_path, config.demo_subsample_interval)
        self._data_loader = torch.utils.data.DataLoader(
            self._dataset, batch_size=self._config.batch_size, shuffle=True
        )
        self._data_iter = iter(self._data_loader)

        # policy dataset
        sampler = RandomSampler()
        self._buffer = ReplayBuffer(
            ["ob", "ac", "done", "rew", "ret", "adv", "ac_before_activation"],
            config.rollout_length,
            sampler.sample_func,
        )

        self._log_creation()

    def predict_reward(self, ob, ac=None):
        ob = self.normalize(ob)
        ob = to_tensor(ob, self._config.device)
        if self._config.gail_no_action:
            ac = None
        if ac is not None:
            ac = to_tensor(ac, self._config.device)

        with torch.no_grad():
            ret = self._discriminator(ob, ac)
            eps = 1e-20
            s = torch.sigmoid(ret)
            if self._config.gail_vanilla_reward:
                reward = -(1 - s + eps).log()
            else:
                reward = (s + eps).log() - (1 - s + eps).log()
        return reward.cpu().item()

    def _log_creation(self):
        if self._config.is_chef:
            logger.info("Creating a GAIL agent")
            logger.info("The actor has %d parameters", count_parameters(self._actor))

    def store_episode(self, rollouts):
        self._compute_gae(rollouts)
        self._buffer.store_episode(rollouts)

    def _compute_gae(self, rollouts):
        T = len(rollouts["done"])
        ob = rollouts["ob"]
        ob = self.normalize(ob)
        ob = obs2tensor(ob, self._config.device)
        vpred = self._critic(ob).detach().cpu().numpy()[:, 0]
        assert len(vpred) == T + 1

        done = rollouts["done"]
        rew = rollouts["rew"]
        adv = np.empty((T,), "float32")
        lastgaelam = 0
        for t in reversed(range(T)):
            nonterminal = 1 - done[t]
            delta = (
                rew[t]
                + self._config.rl_discount_factor * vpred[t + 1] * nonterminal
                - vpred[t]
            )
            adv[t] = lastgaelam = (
                delta
                + self._config.rl_discount_factor
                * self._config.gae_lambda
                * nonterminal
                * lastgaelam
            )

        ret = adv + vpred[:-1]

        assert np.isfinite(adv).all()
        assert np.isfinite(ret).all()

        # update rollouts
        rollouts["adv"] = ((adv - adv.mean()) / adv.std()).tolist()
        rollouts["ret"] = ret.tolist()

    def state_dict(self):
        return {
            "actor_state_dict": self._actor.state_dict(),
            "critic_state_dict": self._critic.state_dict(),
            "discriminator_state_dict": self._discriminator.state_dict(),
            "actor_optim_state_dict": self._actor_optim.state_dict(),
            "critic_optim_state_dict": self._critic_optim.state_dict(),
            "discriminator_optim_state_dict": self._discriminator_optim.state_dict(),
            "ob_norm_state_dict": self._ob_norm.state_dict(),
        }

    def load_state_dict(self, ckpt):
        if "critic_state_dict" not in ckpt:
            # BC initialization
            logger.warn("Load only actor from BC initialization")
            self._actor.load_state_dict(ckpt["actor_state_dict"], strict=False)
            self._network_cuda(self._config.device)
            self._ob_norm.load_state_dict(ckpt["ob_norm_state_dict"])
            return

        self._actor.load_state_dict(ckpt["actor_state_dict"])
        self._critic.load_state_dict(ckpt["critic_state_dict"])
        self._discriminator.load_state_dict(ckpt["discriminator_state_dict"])
        self._ob_norm.load_state_dict(ckpt["ob_norm_state_dict"])
        self._network_cuda(self._config.device)

        self._actor_optim.load_state_dict(ckpt["actor_optim_state_dict"])
        self._critic_optim.load_state_dict(ckpt["critic_optim_state_dict"])
        self._discriminator_optim.load_state_dict(
            ckpt["discriminator_optim_state_dict"]
        )
        optimizer_cuda(self._actor_optim, self._config.device)
        optimizer_cuda(self._critic_optim, self._config.device)
        optimizer_cuda(self._discriminator_optim, self._config.device)

    def _network_cuda(self, device):
        self._actor.to(device)
        self._old_actor.to(device)
        self._critic.to(device)
        self._discriminator.to(device)

    def sync_networks(self):
        sync_networks(self._actor)
        sync_networks(self._old_actor)
        sync_networks(self._critic)
        sync_networks(self._discriminator)

    def train(self):
        train_info = Info()

        self._actor_lr_scheduler.step()
        self._critic_lr_scheduler.step()
        self._discriminator_lr_scheduler.step()

        self._copy_target_network(self._old_actor, self._actor)

        batch_size = self._config.batch_size
        num_batches = self._config.ppo_epoch * self._config.rollout_length // self._config.batch_size
        for _ in range(num_batches):
            policy_data = self._buffer.sample(batch_size)
            _train_info = self._update_policy(policy_data)
            train_info.add(_train_info)

        num_batches = self._config.rollout_length // self._config.batch_size // self._config.discriminator_update_freq
        for _ in range(num_batches):
            policy_data = self._buffer.sample(batch_size)
            try:
                expert_data = next(self._data_iter)
            except StopIteration:
                self._data_iter = iter(self._data_loader)
                expert_data = next(self._data_iter)
            _train_info = self._update_discriminator(policy_data, expert_data)
            train_info.add(_train_info)

        self._buffer.clear()

        train_info.add(
            {
                "actor_grad_norm": compute_gradient_norm(self._actor),
                "actor_weight_norm": compute_weight_norm(self._actor),
                "critic_grad_norm": compute_gradient_norm(self._critic),
                "critic_weight_norm": compute_weight_norm(self._critic),
            }
        )
        return train_info.get_dict(only_scalar=True)

    def _update_discriminator(self, policy_data, expert_data):
        info = Info()

        _to_tensor = lambda x: to_tensor(x, self._config.device)
        # pre-process observations
        p_o = policy_data["ob"]
        p_o = self.normalize(p_o)

        p_bs = len(policy_data["ac"])
        p_o = _to_tensor(p_o)
        if self._config.gail_no_action:
            p_ac = None
        else:
            p_ac = _to_tensor(policy_data["ac"])

        e_o = expert_data["ob"]
        e_o = self.normalize(e_o)

        e_bs = len(expert_data["ac"])
        e_o = _to_tensor(e_o)
        if self._config.gail_no_action:
            e_ac = None
        else:
            e_ac = _to_tensor(expert_data["ac"])

        p_logit = self._discriminator(p_o, p_ac)
        e_logit = self._discriminator(e_o, e_ac)

        p_output = torch.sigmoid(p_logit)
        e_output = torch.sigmoid(e_logit)

        p_loss = self._discriminator_loss(
            p_logit, torch.zeros_like(p_logit).to(self._config.device)
        )
        e_loss = self._discriminator_loss(
            e_logit, torch.ones_like(e_logit).to(self._config.device)
        )

        logits = torch.cat([p_logit, e_logit], dim=0)
        entropy = torch.distributions.Bernoulli(logits).entropy().mean()
        entropy_loss = -self._config.gail_entropy_loss_coeff * entropy

        gail_loss = p_loss + e_loss + entropy_loss

        # update the discriminator
        self._discriminator.zero_grad()
        gail_loss.backward()
        # torch.nn.utils.clip_grad_norm_(self._actor.parameters(), self._config.max_grad_norm)
        sync_grads(self._discriminator)
        self._discriminator_optim.step()

        info["gail_policy_output"] = p_output.mean().detach().cpu().item()
        info["gail_expert_output"] = e_output.mean().detach().cpu().item()
        info["gail_entropy"] = entropy.detach().cpu().item()
        info["gail_policy_loss"] = p_loss.detach().cpu().item()
        info["gail_expert_loss"] = e_loss.detach().cpu().item()
        info["gail_entropy_loss"] = entropy_loss.detach().cpu().item()

        return mpi_average(info.get_dict(only_scalar=True))

    def _update_policy(self, transitions):
        info = Info()

        # pre-process observations
        o = transitions["ob"]
        o = self.normalize(o)

        bs = len(transitions["done"])
        _to_tensor = lambda x: to_tensor(x, self._config.device)
        o = _to_tensor(o)
        ac = _to_tensor(transitions["ac"])
        a_z = _to_tensor(transitions["ac_before_activation"])
        ret = _to_tensor(transitions["ret"]).reshape(bs, 1)
        adv = _to_tensor(transitions["adv"]).reshape(bs, 1)

        _, _, log_pi, ent = self._actor.act(o, activations=a_z, return_log_prob=True)
        _, _, old_log_pi, _ = self._old_actor.act(o, activations=a_z, return_log_prob=True)
        if old_log_pi.min() < -100:
            logger.error("sampling an action with a probability of 1e-100")
            import ipdb

            ipdb.set_trace()

        # the actor loss
        entropy_loss = self._config.entropy_loss_coeff * ent.mean()
        ratio = torch.exp(log_pi - old_log_pi)
        surr1 = ratio * adv
        surr2 = (
            torch.clamp(
                ratio, 1.0 - self._config.ppo_clip, 1.0 + self._config.ppo_clip
            )
            * adv
        )
        actor_loss = -torch.min(surr1, surr2).mean()

        if (
            not np.isfinite(ratio.cpu().detach()).all()
            or not np.isfinite(adv.cpu().detach()).all()
        ):
            import ipdb

            ipdb.set_trace()
        info["entropy_loss"] = entropy_loss.cpu().item()
        info["actor_loss"] = actor_loss.cpu().item()
        actor_loss += entropy_loss

        # the q loss
        value_pred = self._critic(o)
        value_loss = self._config.value_loss_coeff * (ret - value_pred).pow(2).mean()

        info["value_target"] = ret.mean().cpu().item()
        info["value_predicted"] = value_pred.mean().cpu().item()
        info["value_loss"] = value_loss.cpu().item()

        # update the actor
        self._actor_optim.zero_grad()
        actor_loss.backward()
        # torch.nn.utils.clip_grad_norm_(self._actor.parameters(), self._config.max_grad_norm)
        sync_grads(self._actor)
        self._actor_optim.step()

        # update the critic
        self._critic_optim.zero_grad()
        value_loss.backward()
        # torch.nn.utils.clip_grad_norm_(self._critic1.parameters(), self._config.max_grad_norm)
        sync_grads(self._critic)
        self._critic_optim.step()

        # include info from policy
        info.add(self._actor.info)

        return mpi_average(info.get_dict(only_scalar=True))
