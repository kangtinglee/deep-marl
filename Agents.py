import torch as T
import torch.utils as U
import torch.nn.functional as F
import numpy as np
from ReplayBuffer import ReplayBuffer
import os


class MADDPGAgent(object):

    def __init__(self, id, local_q, model, num_units, lr, gamma, tau, batch_size, min_replay_size, replay_buffer_size, update_interval, obs_shape, act_shape, n, global_observation_shape, global_action_shape):

        # Metadata
        self.id = id
        self.device = T.device('cuda' if T.cuda.is_available() else 'cpu')
        self.n = n

        # ReplayBuffer
        self.min_replay_size = min_replay_size
        self.replay_buffer_size = replay_buffer_size
        self.replay_buffer = ReplayBuffer(max_capacity=self.replay_buffer_size, obs_shape=obs_shape, act_shape=act_shape)

        # Training parameters
        self.update_interval = update_interval
        self.gamma = gamma
        self.tau = tau
        self.lr = lr
        self.local_q = local_q
        self.batch_size = batch_size
        self.obs_shape = obs_shape
        self.act_shape = act_shape

        # Networks
        self.pi = model(self.lr, self.obs_shape, self.act_shape, T.tanh, self.device, num_units)
        self.pi_target = model(self.lr, self.obs_shape, self.act_shape, T.tanh, self.device, num_units)
        self.pi_optimizer = self.pi.optimizer

        if self.local_q:
            q_input_shape = (sum(self.obs_shape + self.act_shape),)
        else:
            input_shape = []
            for space in global_observation_shape:
                input_shape.append(space.shape[0])
            for space in global_action_shape:
                input_shape.append(space.n)
            q_input_shape = (sum(input_shape),)
        self.q = model(self.lr, q_input_shape, (1,), lambda x: x, self.device, num_units)
        self.q_target = model(self.lr, q_input_shape, (1,), lambda x: x, self.device, num_units)
        self.q_optimizer = self.q.optimizer

        self.sync_weights(1.0)

    def experience(self, obs, act, rew, new_obs, done):
        self.replay_buffer.add(obs, act, rew, new_obs, done)

    def get_action(self, obs):
        if type(obs) == np.ndarray:
            obs = T.tensor(obs, dtype=T.float32).unsqueeze(0).to(self.device)
        return self.pi(obs)

    def get_target_action(self, obs):
        if type(obs) == np.ndarray:
            obs = T.tensor(obs, dtype=T.float32).unsqueeze(0).to(self.device)
        return self.pi_target(obs)

    def get_experience(self, idx):
        return self.replay_buffer.get_experience(idx)

    def sync_weights(self, amount):
        with T.no_grad():
            for pi_param, target_pi_param in zip(self.pi.parameters(), self.pi_target.parameters()):
                target_pi_param.data = amount * pi_param.data + (1 - amount) * target_pi_param.data

            for q_param, target_q_param in zip(self.q.parameters(), self.q_target.parameters()):
                target_q_param.data = amount * q_param.data + (1 - amount) * target_q_param.data

    def update(self, agents, t):
        # Check if it is time to update
        if t % self.update_interval != 0:
            return None

        if self.local_q:
            agents = [self]

        info = {}

        replay_buffer_size = self.replay_buffer.get_size()

        # Update Q function
        sampled_idx = np.random.choice(np.arange(0, replay_buffer_size), self.batch_size)
        # Calculate target, actual Qs
        global_obs = []
        global_actions = []
        for agent in agents:
            obs, act, rew, new_obs, done = agent.get_experience(sampled_idx)
            new_obs = T.tensor(new_obs, dtype=T.float32).to(self.device)
            global_obs.append(new_obs)
            global_actions.append(agent.pi_target(new_obs))
        global_obs = T.cat(global_obs, 1).to(self.device)
        global_actions = T.cat(global_actions, 1).to(self.device)
        q_input = T.cat([global_obs,global_actions], 1)
        _, _, rew, _, done = self.get_experience(sampled_idx)
        rew = T.tensor(rew, dtype=T.float32).to(self.device)
        done = T.tensor(done, dtype=T.float32).to(self.device)
        actual = rew + self.gamma * (1 - done) * self.q_target(q_input)

        # Calculate predicted Qs
        global_obs = []
        global_actions = []
        for agent in agents:
            obs, act, rew, new_obs, done = agent.get_experience(sampled_idx)
            obs = T.tensor(obs, dtype=T.float32).to(self.device)
            act = T.tensor(act, dtype=T.float32).to(self.device)
            global_obs.append(obs)
            global_actions.append(act)
        global_obs = T.cat(global_obs, 1).to(self.device)
        global_actions = T.cat(global_actions, 1).to(self.device)
        q_input = T.cat([global_obs,global_actions], 1)
        predicted = self.q(q_input)

        loss = T.mean(T.pow(actual - predicted, 2))

        info['q_loss'] = loss

        self.q_optimizer.zero_grad()
        loss.backward()
        self.q_optimizer.step()

        # Update Pi
        global_obs = []
        global_actions = []
        for agent in agents:
            obs, act, rew, new_obs, done = agent.get_experience(sampled_idx)
            obs = T.tensor(obs, dtype=T.float32).to(self.device)
            global_obs.append(obs)
            global_actions.append(agent.pi(obs))
        global_obs = T.cat(global_obs, 1).to(self.device)
        global_actions = T.cat(global_actions, 1).to(self.device)
        q_input = T.cat([global_obs, global_actions], 1)

        loss = -T.mean(self.q(q_input))

        info['pi_loss'] = loss

        self.pi_optimizer.zero_grad()
        loss.backward()
        self.pi_optimizer.step()

        self.sync_weights(self.tau)

        return info
    
    def save_agent(self, save_path):
        if not os.path.exists(save_path):
            os.mkdir(save_path)

        target_pi_path = os.path.join(save_path, "target_pi_network.pth")
        T.save(self.pi_target.state_dict(), target_pi_path)

        target_q_path = os.path.join(save_path, "target_q_network.pth")
        T.save(self.q_target.state_dict(), target_q_path)

        pi_path = os.path.join(save_path, "pi_network.pth")
        T.save(self.pi.state_dict(), pi_path)

        q_path = os.path.join(save_path, "q_network.pth")
        T.save(self.q.state_dict(), q_path)

    def load_agent(self, save_path):
        pi_path = os.path.join(save_path, "pi_network.pth")
        self.pi.load_state_dict(T.load(pi_path))
        self.pi.eval()

        target_pi_path = os.path.join(save_path, "target_pi_network.pth")
        self.pi_target.load_state_dict(T.load(target_pi_path))
        self.pi_target.eval()

        q_path = os.path.join(save_path, "q_network.pth")
        self.q.load_state_dict(T.load(q_path))
        self.q.eval()

        target_q_path = os.path.join(save_path, "target_q_network.pth")
        self.q_target.load_state_dict(T.load(target_q_path))
        self.q_target.eval()

        self.sync_weights(1.0)