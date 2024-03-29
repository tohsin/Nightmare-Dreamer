import argparse
import collections
import functools
import os
import pathlib
import sys
import warnings
import wandb
if sys.platform == 'linux':
  os.environ['MUJOCO_GL'] = 'egl'

import numpy as np
import ruamel.yaml as yaml

sys.path.append(str(pathlib.Path(__file__).parent))

import ma_exploration as expl
# import ma_models as models
import ma_models_default as models
import ma_tools as tools
import ma_wrappers as wrappers
# import dm_wrapper as wrappers

import torch
from torch import nn
from torch import distributions as torchd
to_np = lambda x: x.detach().cpu().numpy()

# online_mean_cost_calc = tools.OnlineMeanCalculator()
online_mean_cost_calc = tools.RollingMeanCalculator(50)
VideoInteractionSaver = tools.SaveVideoInteraction()

class Dreamer(nn.Module):

  def __init__(self, config, logger, dataset):
    super(Dreamer, self).__init__()
    self._config = config
    self._logger = logger
    self._should_log = tools.Every(config.log_every)
    self._should_train = tools.Every(config.train_every)
    self._should_pretrain = tools.Once()
    self._should_reset = tools.Every(config.reset_every)
    self._should_expl = tools.Until( 
      int(config.expl_until / config.action_repeat)
    )
    self._metrics = {}
    self._step = count_steps(config.traindir)
    self.count_before_switch = 0
    # Schedules.
    config.actor_entropy = (
        lambda x = config.actor_entropy: tools.schedule(x, self._step))
    
        # Schedules.
    
    config.actor_state_entropy = (
        lambda x = config.actor_state_entropy: tools.schedule(x, self._step))
    
    config.imag_gradient_mix = (
        lambda x=config.imag_gradient_mix: tools.schedule(x, self._step))
    
    self._dataset = dataset
    self._wm = models.WorldModel(self._step, config)
    self._task_behavior = models.ImagBehavior(
        config, self._wm, config.behavior_stop_grad)
    #inline function to get the reward prediction using world model, not sure why we need it though
    reward = lambda f, s, a: self._wm.heads['reward'](f).mean 
    self._expl_behavior = dict( # greedy which is using actor policy
        greedy = lambda: self._task_behavior,
        random = lambda: expl.Random(config),
        plan2explore = lambda: expl.Plan2Explore(config, self._wm, reward),
    )[config.expl_behavior]()
    self.number_of_switches = 0

  def __call__(self, obs, reset, state = None, reward = None, cost = None, training = True):
    step = self._step
    if self._should_reset(step):
      state = None
    if state is not None and reset.any():
      self.count_before_switch = 0
      self.number_of_switches = 0
      mask = 1 - reset
      for key in state[0].keys():
        for i in range(state[0][key].shape[0]):
          state[0][key][i] *= mask[i]
      for i in range(len(state[1])):
        state[1][i] *= mask[i]
    if training and self._should_train(step):
      steps = (
          self._config.pretrain if self._should_pretrain()
          else self._config.train_steps)
      
      for _ in range(steps):
        self._train(next(self._dataset))

      if self._should_log(step):
        for name, values in self._metrics.items():
          self._logger.scalar(name, float(np.mean(values)))
          self._metrics[name] = []
        # openl = self._wm.video_pred(next(self._dataset))
        # self._logger.video('train_openl', to_np(openl))
        self._logger.write(fps=True)

    policy_output, state = self._policy(obs, state, training)

    if training:
      self._step += len(reset)
      self._logger.step = self._config.action_repeat * self._step
    return policy_output, state

  def _is_future_safety_violated(self, posterior_t, is_eval = False):
    '''
    Starting from current state we roll out using learned model
    to forcast constraint violation under control policy
    '''
    if self.count_before_switch > 0:
      self.count_before_switch -= 1
      # we only return to possibility to use control policy after number of steps to reach saftey has passed
      return True

    total_cost = 0
    cost_fn = lambda f, s, a: self._wm.heads['cost'](f).mode()
        # self._wm.dynamics.get_feat(s)  ).mode()
    with torch.no_grad():
        latent_state = posterior_t
        for _ in range(self._config.safety_look_ahead_steps):
            feat = self._wm.dynamics.get_feat(latent_state)
            c_t = cost_fn(feat,_,_).item()
            total_cost += c_t
            actor = self._task_behavior.actor(feat)
            action = actor.sample() if not is_eval  else actor.mode()
            latent_state = self._wm.dynamics.img_step(latent_state, action, sample = self._config.imag_sample)
    #return total_cost >= self._config.cost_threshold

    is_violation = total_cost >= self._config.cost_threshold

    if is_violation: 
      self.count_before_switch = self._config.num_safety_steps
      self.count_before_switch -= 1 # we are using one safety task so we reduce it
      return True
    return False

  def get_safe_action(self, latent):
    if self._config.sample_safe_action:
      population = [] # (first_action, cost sum, value[t+H])
      cost_fn = lambda f, s, a: self._wm.heads['cost'](f).mode()
      number_candidate = 0 # number of agents that dont violate at all
      for n in range(self._config.num_sampled_action):
        total_cost = 0
        for h in range(self._config.safety_look_ahead_steps):
            feat = self._wm.dynamics.get_feat(latent)
            c_t = cost_fn(feat, None , None).item()
            total_cost += c_t
            action = self._task_behavior.safe_actor(feat).sample()
            if h == 0: # first state
              first_action = action
            latent_state = self._wm.dynamics.img_step(latent_state, action, sample = self._config.imag_sample)
            if h == self._config.safety_look_ahead_steps - 1: # last time step
              #get the value
              value = self._task_behavior.value(feat)
              if total_cost <= 2:
                number_candidate += 1
              population.append(first_action, value, total_cost)

      population.sort(key= lambda x : x[2])
      if number_candidate >= self._config.N: # we have enough candidate agents so we just pick action with best value
        candidates = population[:number_candidate]
        candidates.sort(candidates, key=[1])
        return candidates[0]
      else: # we couldnt find enough candidate action so we select safest action
        return population[0][0]
    else:
      feat = self._wm.dynamics.get_feat(latent)
      self._task_behavior.safe_actor(feat).sample()

  def _task_switch_prob(self):
    '''
    returns the probability of selecting the control policy or forcasting ahead
    to make to check for constarint violation and decide which to policy to use
    '''
    if self._logger.step <= self._config.safe_decay_start:
        expl_amount = self._config.safe_signal_prob
    else:
        expl_amount =  self._config.safe_signal_prob
        ir = self._logger.step  - self._config.safe_decay_start + 1
        expl_amount = expl_amount - ir/self._config.safe_signal_prob_decay
        expl_amount = max(self._config.safe_signal_prob_decay_min, expl_amount)
    self._logger._scalars['Safe_policy_switch_prob'] =  expl_amount
    return expl_amount
    
  def _policy(self, obs, state, training):
    if state is None:
      batch_size = len(obs['image'])
      latent = self._wm.dynamics.initial(len(obs['image']))
      action = torch.zeros((batch_size, self._config.num_actions)).to(self._config.device)
    else:
      latent, action = state
    # check with actor to use
    embed = self._wm.encoder(self._wm.preprocess(obs))
    # latent_t = input( embed_t, action_{t-1}, latent_{t-1}
    latent, _ = self._wm.dynamics.obs_step(
        latent, action, embed, self._config.collect_dyn_sample)

    #begin roll out from here under control policy to check for violation, t -1 steps
    if self._config.eval_state_mean:
      latent['stoch'] = latent['mean']
    feat = self._wm.dynamics.get_feat(latent)
    
    if not self._config.solve_cmdp:
      constraint_violated = False
      
    elif self.number_of_switches > self._config.switch_budget:
      if not training: #ignore cap
        constraint_violated = self._is_future_safety_violated(latent)
      else:
        constraint_violated = False

    elif np.random.uniform(0, 1) < self._task_switch_prob():
      constraint_violated = False

    else:
      constraint_violated = self._is_future_safety_violated(latent)
    # constraint_violated = False if np.random.uniform(0, 1) < self._task_switch_prob() \
    #                               else self._is_future_safety_violated(latent)
    if self._config.only_safe_policy:
      constraint_violated = True
    self.number_of_switches += 1 if constraint_violated else 0

    if not training:
      #in this case no need for epsilon greedy
      actor =  self._task_behavior.safe_actor(feat) if constraint_violated \
              else self._task_behavior.actor(feat)
      action = actor.mode()

    elif self._should_expl(self._step):
      actor =  self._expl_behavior.safe_actor(feat) if constraint_violated \
              else self._expl_behavior.actor(feat)
      action = actor.sample()

      # actor =  self._expl_behavior.safe_actor(feat) if constraint_violated \
      #         else self._expl_behavior.actor(feat)
      
      # action = self.get_safe_action if constraint_violated else actor.sample()
    else:
      actor =  self._task_behavior.safe_actor(feat) if constraint_violated \
              else self._task_behavior.actor(feat)
      action = actor.sample()

    logprob = actor.log_prob(action)
    latent = {k: v.detach()  for k, v in latent.items()}
    action = action.detach()
    if self._config.actor_dist == 'onehot_gumble':
      action = torch.one_hot(torch.argmax(action, dim=-1), self._config.num_actions)
    action = self._exploration(action, training)
    num_task_switch = 1 if constraint_violated else 0
    policy_output = {'action': action, 'logprob': logprob, 'task_switch' : torch.tensor([num_task_switch])}
    state = (latent, action)
    return policy_output, state

  def _exploration(self, action, training):
    amount = self._config.expl_amount if training else self._config.eval_noise
    if amount == 0:
      return action
    if 'onehot' in self._config.actor_dist:
      probs = amount / self._config.num_actions + (1 - amount) * action
      return tools.OneHotDist(probs=probs).sample()
    else:
      return torch.clip(torchd.normal.Normal(action, amount).sample(), -1, 1)
    raise NotImplementedError(self._config.action_noise)

  def _train(self, data):
    metrics = {}
    # train world model
    post, context, mets = self._wm._train(data)
    metrics.update(mets)
    start = post
    if self._config.pred_discount:  # Last step could be terminal.
      start = {k: v[:, :-1] for k, v in post.items()}
      context = {k: v[:, :-1] for k, v in context.items()}

    reward = lambda f, s, a: self._wm.heads['reward'](
        self._wm.dynamics.get_feat(s)).mode()
    cost = lambda f, s, a: self._wm.heads['cost'](
        self._wm.dynamics.get_feat(s)).mode()
    metrics.update(self._task_behavior._train(start, reward, cost, mean_ep_cost = online_mean_cost_calc.get_mean(), training_step = self._logger.step )[-1])

    if self._config.expl_behavior != 'greedy':
      if self._config.pred_discount:
        data = {k: v[:, :-1] for k, v in data.items()}
      mets = self._expl_behavior.train(start, context, data)[-1]
      metrics.update({'expl_' + key: value for key, value in mets.items()})
    #update training metrics for logs
    for name, value in metrics.items():
      if not name in self._metrics.keys():
        self._metrics[name] = [value]
      else:
        self._metrics[name].append(value)


def count_steps(folder):
  '''
  - find all files with extension .npz convert to string
  - COunt the file names wit that extension to know number of steps

  '''
  return sum(int(str(n).split('-')[-1][:-4]) - 1 for n in folder.glob('*.npz'))


def make_dataset(episodes, config):
  generator = tools.sample_episodes(
      episodes, config.batch_length, config.oversample_ends)
  dataset = tools.from_generator(generator, config.batch_size)
  return dataset


def make_env(config, logger, mode, train_eps, eval_eps):
  if config.task_type == 'dmc':
    env = wrappers.DMGymnassium(config.task, config.grayscale, action_repeat = config.action_repeat )
  else:
    env = wrappers.SafetyGym(config.task, config.grayscale, action_repeat = config.action_repeat ) if not config.ontop else \
      wrappers.SafetyGym(config.task, config.grayscale, action_repeat = config.action_repeat, camera_name = 'fixednear' )
  
  env = wrappers.NormalizeActions(env)
  env = wrappers.TimeLimit(env, config.time_limit)
  env = wrappers.SelectAction(env, key='action')
  if (mode == 'train') or (mode == 'eval'):
    callbacks = [functools.partial(
        process_episode, config, logger, mode, train_eps, eval_eps)]
    env = wrappers.CollectDataset(env, callbacks)
  env = wrappers.RewardObs(env)
  env = wrappers.CostObs(env)
  return env


def process_episode(config, logger, mode, train_eps, eval_eps, episode):
  directory = dict(train = config.traindir, eval = config.evaldir)[mode]
  cache = dict(train = train_eps, eval = eval_eps)[mode]
  filename = tools.save_episodes(directory, [episode])[0]
  length = len(episode['reward']) - 1
  score = float(episode['reward'].astype(np.float64).sum())
  score_cost = float(episode['cost'].astype(np.float64).sum())
  num_task_switch = float(episode['task_switch'].astype(np.float64).sum())
  video = episode['image']
#  VideoInteractionSaver.save_video(video, score, score_cost, episode['task_switch'])
  if mode == 'eval':
    VideoInteractionSaver.save_video(video, score, score_cost, episode['task_switch'])
    cache.clear()
  if mode == 'train' and config.dataset_size:
    total = 0
    for key, ep in reversed(sorted(cache.items(), key=lambda x: x[0])):
      if total <= config.dataset_size - length:
        total += len(ep['reward']) - 1
      else:
        del cache[key]
    logger.scalar('dataset_size', total + length)
  cache[str(filename)] = episode
  print(f'{mode.title()} episode has {length} steps, return {score:.1f}, cost {score_cost:.1f} and algorithm switched task {num_task_switch:.1f} times to safe agent.')
  if mode == 'train':
    online_mean_cost_calc.update(score_cost)
  logger.scalar('Online Mean Cost', online_mean_cost_calc.get_mean())
  logger.scalar(f'{mode}_cost_return', score_cost)
  logger.scalar(f'{mode}_num_task_switch', num_task_switch)
  logger.scalar(f'{mode}_return', score)
  logger.scalar(f'{mode}_length', length)
  logger.scalar(f'{mode}_episodes', len(cache))
  if mode == 'eval' or config.expl_gifs:
    logger.video(f'{mode}_policy', video[None])
  logger.write()


def set_test_paramters(config):
  # For testing on my mac to prevent high ram usage
  config.debug = True
  config.pretrain =  1
  config.prefill = 1
  config.train_steps = 1
  config.batch_size = 10
  config.batch_length = 20

def main(config):
  config_dict = config.__dict__
  config.task_type = '' # dmc or eempty string
  #dmc Humanoid-v4 'Hopper-v4'
  # 'Hopper-v4' SafetyWalker2dVelocity 'SafetyHalfCheetahVelocity-v1' 'SafetyPointCircle1-v0' SafetySwimmerVelocity-v1
  config.task = 'SafetyPointPush1-v0'  #HalfCheetah-v4
  config.steps = 1e7
  config.device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
  if sys.platform != 'linux': set_test_paramters(config)# if not zhuzun running so parameters for testing locally
  # print(config_dict)
  if sys.platform == 'linux': #not debugging on mac but running experiment

    # run =  wandb.init(project='Safe RL via Latent world models Setup mac', config = config_dict) \
    # if sys.platform != 'linux' else wandb.init(project='Safe RL via Latent world models Setup', config = config_dict)

    run = wandb.init(project='Safe RL via Latent world models Setup', config = config_dict)
    #run = wandb.init(project='Nightmare Dreamer', config = config_dict)
  logdir = pathlib.Path(config.logdir).expanduser()
  config.traindir = config.traindir or logdir / 'train_eps'
  config.evaldir = config.evaldir or logdir / 'eval_eps'
  config.steps //= config.action_repeat
  config.eval_every //= config.action_repeat
  config.log_every //= config.action_repeat
  config.time_limit //= config.action_repeat
  config.act = getattr(torch.nn, config.act) #activation layer

  print('Logdir', logdir)
  logdir.mkdir(parents = True, exist_ok = True)
  VideoInteractionSaver.set_video_dir(logdir, config.logdir)
  config.traindir.mkdir(parents=True, exist_ok=True)
  config.evaldir.mkdir(parents=True, exist_ok=True)
  step = count_steps(config.traindir)
  logger = tools.Logger(logdir, config.action_repeat * step)

  print('Create envs.')
  if config.offline_traindir:
    directory = config.offline_traindir.format(**vars(config))
  else:
    directory = config.traindir
  train_eps = tools.load_episodes(directory, limit=config.dataset_size)

  if config.offline_evaldir:
    directory = config.offline_evaldir.format(**vars(config))
  else:
    directory = config.evaldir

  eval_eps = tools.load_episodes(directory, limit=1)
  make = lambda mode: make_env(config, logger, mode, train_eps, eval_eps)
  train_envs = [make('train') for _ in range(config.envs)]
  eval_envs = [make('eval') for _ in range(config.envs)]
  acts = train_envs[0].action_space
  config.num_actions = acts.n if hasattr(acts, 'n') else acts.shape[0]

  if not config.offline_traindir: 
    prefill = max(0, config.prefill - count_steps(config.traindir))
    print(f'Prefill dataset ({prefill} steps).')
    if hasattr(acts, 'discrete'):
      random_actor = tools.OneHotDist(torch.zeros_like(torch.Tensor(acts.low))[None])
    else:
      random_actor = torchd.independent.Independent(
          torchd.uniform.Uniform(torch.Tensor(acts.low)[None],
                                torch.Tensor(acts.high)[None]), 1)
    def random_agent(o, d, s, r, c):
      action = random_actor.sample()
      logprob = random_actor.log_prob(action)
      return {'action': action, 'logprob': logprob, 'task_switch' : torch.tensor([0])}, None
    tools.simulate(random_agent, train_envs, prefill)
    tools.simulate(random_agent, eval_envs, episodes=1)
    logger.step = config.action_repeat * count_steps(config.traindir)

  print('Simulate agent.')
  train_dataset = make_dataset(train_eps, config)
  eval_dataset = make_dataset(eval_eps, config)
  #intialise world models, and imgination(actor, critic)
  agent = Dreamer(config, logger, train_dataset).to(config.device)
  agent.requires_grad_(requires_grad = False)
  if (logdir / 'latest_model.pt').exists():
    agent.load_state_dict(torch.load(logdir / 'latest_model.pt'))
    agent._should_pretrain._once = False

  state = None
  while agent._step < config.steps:
    logger.write()
    print('Start evaluation.')
    video_pred = agent._wm.video_pred(next(eval_dataset))
    logger.video('eval_openl', to_np(video_pred))
    eval_policy = functools.partial(agent, training = False)
    tools.simulate(eval_policy, eval_envs, episodes = 1)
    print('Start training.')
    # this rolls out mdps, and adds state to the buffer inside the 
    # agent as well as get the action through the states
    state = tools.simulate(agent, train_envs, config.eval_every, state = state)
    torch.save(agent.state_dict(), logdir / 'latest_model.pt')
  for env in train_envs + eval_envs:
    try:
      env.close()
    except Exception:
      pass


if __name__ == '__main__':
  parser = argparse.ArgumentParser()
  # parser.add_argument('--configs', nargs='+', required=True)
  parser.add_argument('--configs', nargs='+', default=['defaults', 'sgym'], required=False)
  args, remaining = parser.parse_known_args()
  configs = yaml.safe_load(
      (pathlib.Path(sys.argv[0]).parent / 'ma_configs.yaml').read_text())
  defaults = {}
  for name in args.configs:
    defaults.update(configs[name])
  parser = argparse.ArgumentParser()
  for key, value in sorted(defaults.items(), key = lambda x: x[0]):
    arg_type = tools.args_type(value)
    parser.add_argument(f'--{key}', type=arg_type, default=arg_type(value))
  current_dir = os.path.dirname(os.path.abspath(__file__))
  # For linux running 
  logdir = os.path.join(current_dir, 'logdir', 'safecircle1', '0')
  existed_ns = [int(v) for v in os.listdir(os.path.join(current_dir, 'logdir', 'safecircle1'))]
  if len(existed_ns) > 0:
    new_n = max(existed_ns)+1
    logdir = os.path.join(current_dir, 'logdir', 'safecircle1', str(new_n))

  # For jason running un comment
  #logdir = os.path.join(current_dir, 'logdir', 'halfcheetah', '0')
  parser.set_defaults(logdir = logdir)
  main(parser.parse_args(remaining))
