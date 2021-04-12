import random
import argparse
import os
import torch
import time
import numpy as np

import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader

from model import MCActor, Critic
from environment import WRSNEnv
from utils import NetworkInput, WRSNDataset, Point
from utils import Config, DrlParameters as dp, WrsnParameters as wp
from utils import logger, gen_cgrg, device


def train(actor, critic, train_data, valid_data, save_dir):
    train_loader = DataLoader(train_data, 1, True, num_workers=0)
    valid_loader = DataLoader(valid_data, 1, False, num_workers=0)

    actor_optim = optim.Adam(actor.parameters(), dp.actor_lr)
    critic_optim = optim.Adam(critic.parameters(), dp.critic_lr)

    for epoch in range(dp.num_epoch):
        actor.train()
        critic.train()

        epoch_start = time.time()
        start = epoch_start

        mean_policy_losses = []
        mean_entropies = []
        times = [0]
        net_lifetimes = []
        mc_travel_dists = []

        for idx, data in enumerate(train_loader):
            sensors, targets = data

            env = WRSNEnv(sensors=sensors.squeeze(), 
                          targets=targets.squeeze(), 
                          normalize=True)

            mc_state, sn_state = env.reset()
            mc_state = torch.from_numpy(mc_state).to(dtype=torch.float32, device=device)
            sn_state = torch.from_numpy(sn_state).to(dtype=torch.float32, device=device)

            values = []
            log_probs = []
            rewards = []
            entropies = []

            mask = torch.ones(env.action_space.n)

            for _ in range(dp.max_step):
                mc_state = mc_state.unsqueeze(0)
                sn_state = sn_state.unsqueeze(0)

                logit = actor(mc_state, sn_state)
                logit = logit + mask.log()
                # print(logit)

                prob = F.softmax(logit, dim=-1)
                log_prob = F.log_softmax(logit, dim=-1)
                entropy = -(log_prob * prob).sum(1, keepdim=True)
                # print(prob)
                # print(log_prob)
                # print(entropy)

                value = critic(mc_state, sn_state)

                if actor.training:
                    m = torch.distributions.Categorical(prob)

                    # Sometimes an issue with Categorical & sampling on GPU; See:
                    # https://github.com/pemami4911/neural-combinatorial-rl-pytorch/issues/5
                    action = m.sample()
                    logp = m.log_prob(action)
                else:
                    prob, action = torch.max(prob, 1)  # Greedy selection
                    logp = prob.log()

                # mask[env.last_action] = 1.
                (mc_state, sn_state), reward, done, info = env.step(action.squeeze().item())
                # mask[env.last_action] = 0.

                mc_state = torch.from_numpy(mc_state).to(dtype=torch.float32, device=device)
                sn_state = torch.from_numpy(sn_state).to(dtype=torch.float32, device=device)

                values.append(value) 
                rewards.append(reward)
                log_probs.append(logp)
                entropies.append(entropy)

                if done:
                    env.close()
                    break

            net_lifetimes.append(env.get_network_lifetime())
            mc_travel_dists.append(env.get_travel_distance())

            R = torch.zeros(1, 1)
            if not done:
                value = critic(mc_state.unsqueeze(0), sn_state.unsqueeze(0))
                R = value.detach()

            values.append(R)
            
            gae = torch.zeros(1, 1)
            policy_losses = torch.zeros(len(rewards))
            value_losses = torch.zeros(len(rewards))

            for i in reversed(range(len(rewards))):
                reward = rewards[i][0] # using time only
                R = dp.gamma * R + reward
                advantage = R - values[i]

                value_losses[i] = 0.5 * advantage.pow(2)

                # Generalized Advantage Estimation
                delta_t = reward + dp.gamma * \
                    values[i + 1] - values[i]
                gae = gae * dp.gamma * dp.gae_lambda + delta_t

                policy_losses[i] = -log_probs[i] * gae.detach() - \
                                     dp.entropy_coef * entropies[i]

            actor_optim.zero_grad()
            policy_losses.sum().backward()
            torch.nn.utils.clip_grad_norm_(actor.parameters(), dp.max_grad_norm)
            actor_optim.step()

            critic_optim.zero_grad()
            value_losses.sum().backward()
            torch.nn.utils.clip_grad_norm_(critic.parameters(), dp.max_grad_norm)
            critic_optim.step()
            
            with torch.no_grad():
                mean_policy_losses.append(torch.mean(policy_losses).item())
                mean_entropies.append(torch.mean(torch.Tensor(entropies)).item())

            if (idx + 1) % 100 == 0:
                end = time.time()
                times.append(end-start)
                start = end

                mm_policy_loss = np.mean(mean_policy_losses[-100:])
                mm_entropies = np.mean(mean_entropies[-100:])
                m_net_lifetime = np.mean(net_lifetimes[-100:])
                m_mc_travel_dist = np.mean(mc_travel_dists[-100:])

                msg = 'Batch %d/%d, mean_policy_losses: %2.3f, ' + \
                    'mean_net_lifetime: %2.4f, mean_mc_travel_dist: %2.4f, ' + \
                    'mean_entropies: %2.4f, took: %2.4fs'
                logger.info(msg % (idx, len(train_loader), mm_policy_loss, 
                                   m_net_lifetime, m_mc_travel_dist,
                                   mm_entropies, times[-1]))

        mm_policy_loss = np.mean(mean_policy_losses)
        mm_entropies = np.mean(mean_entropies)
        m_net_lifetime = np.mean(net_lifetimes)
        m_mc_travel_dist = np.mean(mc_travel_dists)

        msg = 'Mean epoch %d: mean_policy_losses: %2.3f, ' + \
            'mean_net_lifetime: %2.4f, mean_mc_travel_dist: %2.4f, ' + \
            'mean_entropies: %2.4f, took: %2.4fs, (%2.4f / 100 batches)\n'
        logger.info(msg % (epoch, mm_policy_loss, m_net_lifetime, 
                           m_mc_travel_dist, mm_entropies,
                           time.time() - epoch_start, np.mean(times)))




def main(num_sensors=20, num_targets=10, config=None,
          checkpoint=None, save_dir='checkpoints', seed=123, mode='train'):
    # logger.info("Training problem with %d sensors %d targets (checkpoint: %s) ()")
    if config is not None:
        wp.from_file(config)
        dp.from_file(config)

    save_dir = os.path.join(save_dir, f'mc_{num_sensors}_{num_targets}')

    train_data = WRSNDataset(num_sensors, num_targets, dp.train_size, seed)
    valid_data = WRSNDataset(num_sensors, num_targets, dp.valid_size, seed + 1)


    actor = MCActor(dp.MC_INPUT_SIZE, 
                    dp.SN_INPUT_SIZE,
                    dp.hidden_size,
                    dp.dropout).to(device)

    critic = Critic(dp.MC_INPUT_SIZE,
                    dp.SN_INPUT_SIZE,
                    dp.hidden_size).to(device)

    if checkpoint is not None:
        path = os.path.join(checkpoint, 'actor.pt')
        actor.load_state_dict(torch.load(path, device))

        path = os.path.join(checkpoint, 'critic.pt')
        critic.load_state_dict(torch.load(path, device))

    if mode == 'train':
        train(actor, critic, train_data, valid_data, save_dir)


if __name__ == '__main__':
    
    parser = argparse.ArgumentParser(description="Mobile Charger Trainer")
    parser.add_argument('--num_sensors', '-ns', default=20, type=int)
    parser.add_argument('--num_targets', '-nt', default=10, type=int)
    parser.add_argument('--mode', default='train', type=str, choices=['train', 'eval'])
    parser.add_argument('--config', '-cf', default=None, type=str)
    parser.add_argument('--checkpoint', '-cp', default=None, type=str)
    parser.add_argument('--save_dir', '-sd', default='checkpoints', type=str)

    args = parser.parse_args()

    torch.set_printoptions(sci_mode=False)
    seed = 42
    torch.manual_seed(seed)
    np.set_printoptions(suppress=True)

    main(**vars(args))
    # gen_cgrg(20, 10, np.random.RandomState(1))
