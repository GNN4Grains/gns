import collections
import json
import os
import pickle
import glob
import re
import sys
import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP

import argparse

import logging
logger = logging.getLogger(__name__)

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from gns import learned_simulator_baseline as learned_simulator
from gns import noise_utils
from gns import reading_utils
from gns import data_loader
from gns import distribute

import utils.utils as utils

parser = argparse.ArgumentParser(description='GNS simulator training script')

default_data_path = "/mnt/raid0sata1/hcj/SAG_Mill/Train_Data"

default_ds_name = "SAGMill"
default_res_dir="../results"

default_model_path=f"{default_res_dir}/models/${default_ds_name}/"
default_rollouts_path=f"{default_res_dir}/rollouts/${default_ds_name}/"

parser.add_argument(
    '--mode', type=str, default='train', choices=['train', 'valid', 'rollout'],
    help='Train model, validation or rollout evaluation.')
parser.add_argument('--batch_size', type=int, default=1, help='The batch size.')
parser.add_argument('--noise_std', type=float, default=6.7e-4, help='The std deviation of the noise.')
parser.add_argument('--data_path', type=str, default=default_data_path, help='The dataset directory.')
parser.add_argument('--model_path', type=str, default=default_model_path, help=('The path for saving checkpoints of the model.'))
parser.add_argument('--output_path', type=str, default=default_rollouts_path, help='The path for saving outputs (e.g. rollouts).')
parser.add_argument('--model_file', type=str, default="latest", help=('Model filename (.pt) to resume from. Can also use "latest" to default to newest file.'))
parser.add_argument('--train_state_file', type=str, default="latest", help=('Train state filename (.pt) to resume from. Can also use "latest" to default to newest file.'))
parser.add_argument('--exp_id', type=str, default='test', help='Experiment ID.')
parser.add_argument('--ntraining_steps', type=int, default=int(2E7), help='Number of training steps.')
parser.add_argument('--nvalid_steps', type=int, default=int(2000), help='Number of steps at which to valid the model.')
parser.add_argument('--nsave_steps', type=int, default=int(2000), help='Number of steps at which to save the model.')
parser.add_argument('--nlog_steps', type=int, default=int(100), help='Number of steps at which to log the model.')
# Learning rate parameters
parser.add_argument('--lr_init', type=float, default=0.01, help='Initial learning rate.')
parser.add_argument('--lr_decay', type=float, default=0.001, help='Learning rate decay.')
parser.add_argument('--lr_decay_steps', type=int, default=int(5e6), help='Learning rate decay steps.')
parser.add_argument('--log_path', type=str, default=default_res_dir, help='The path for saving logs.')
parser.add_argument('--is_cuda', type=bool, default=False, help='Whether to use cuda.')

args = parser.parse_args()

Stats = collections.namedtuple('Stats', ['mean', 'std'])

INPUT_SEQUENCE_LENGTH = 6  # So we can calculate the last 5 velocities.
NUM_PARTICLE_TYPES = 1
KINEMATIC_PARTICLE_ID = -1

def rollout(
        simulator: learned_simulator.LearnedCylinderSimulator,
        position: torch.tensor,
        label: torch.tensor,
        particle_types: torch.tensor,
        edge_index: torch.tensor,
        nsteps: int,
        device):
  """Rolls out a trajectory by applying the model in sequence.

  Args:
    simulator: Learned simulator.
    features: Torch tensor features.
    nsteps: Number of steps.
  """
  # print(position.shape)
  initial_positions = position[:, :INPUT_SEQUENCE_LENGTH]
  ground_truth_positions = label

  print(label.shape)
  current_positions = initial_positions
  predictions = []

  for step in range(nsteps):
    # Get next position with shape (nnodes, dim)
    next_position = simulator.predict_positions(
        current_positions,
        particle_types=particle_types,
        edge_index=edge_index
    )

    # Update kinematic particles from prescribed trajectory.
    kinematic_mask = (particle_types == KINEMATIC_PARTICLE_ID).clone().detach().to(device)
    # next_position_ground_truth = ground_truth_positions[:, step]
    kinematic_mask = kinematic_mask.bool()[:, None].expand(-1, current_positions.shape[-1])
    # next_position = torch.where(
    #     kinematic_mask, next_position_ground_truth, next_position)
    predictions.append(next_position)

    # Shift `current_positions`, removing the oldest position in the sequence
    # and appending the next position at the end.
    current_positions = torch.cat(
        [current_positions[:, 1:], next_position[:, None, :]], dim=1)

  # Predictions with shape (time, nnodes, dim)
  predictions = torch.stack(predictions)
  # print(predictions.shape)
  # ground_truth_positions = ground_truth_positions.permute(1, 0, 2)

  loss = (predictions[-1] - ground_truth_positions) ** 2

  output_dict = {
      'initial_positions': initial_positions.permute(1, 0, 2).cpu().numpy(),
      'predicted_rollout': predictions.cpu().numpy(),
      'ground_truth_rollout': ground_truth_positions.cpu().numpy(),
      'particle_types': particle_types.cpu().numpy(),
  }

  return output_dict, loss


def predict(flags):
  """Predict rollouts.

  Args:
    simulator: Trained simulator if not will undergo training.

  """
  device = flags["device"]
  metadata = reading_utils.read_metadata(flags["data_path"])
  simulator = _get_simulator(metadata, flags["noise_std"], flags["noise_std"], device)

  # Load simulator
  if os.path.exists(flags["model_path"] + flags["model_file"]):
    simulator.load(flags["model_path"] + flags["model_file"])
  else:
    raise FileNotFoundError(f'Model file {flags["model_path"] + flags["model_file"]} not found.')
  simulator.to(device)
  simulator.eval()

  # Output path
  if not os.path.exists(flags["output_path"]):
    os.makedirs(flags["output_path"])

  # Use `valid`` set for eval mode if not use `test`
  split = 'test' if flags["mode"] == 'rollout' else 'valid'

  ds = distribute.get_data_distributed_dataloader_mono_baseline(path=flags["data_path"],
                                                                      input_length_sequence=INPUT_SEQUENCE_LENGTH,
                                                                      batch_size=flags["batch_size"],
                                                                      train_ratio=flags["train_ratio"],
                                                                      shuffle=False
                                                                      )

  eval_loss = []
  with torch.no_grad():
    for example_i, ((positions, particle_type, n_particles_per_example, edge_index), labels) in enumerate(ds):
      if example_i > 0:
        break
      positions.to(device)
      particle_type.to(device)
      # n_particles_per_example = torch.tensor([int(n_particles_per_example)], dtype=torch.int32).to(device)

      nsteps = metadata['sequence_length'] - INPUT_SEQUENCE_LENGTH
      # Predict example rollout
      example_rollout, loss = rollout(simulator, positions.to(device), labels.to(device), particle_type.to(device),
                                      edge_index.to(device), nsteps, device)
      # example_rollout = rollout(simulator, positions.to(device), labels.to(device), particle_type.to(device),
      #                                 edge_index.to(device), nsteps, device)

      example_rollout['metadata'] = metadata
      print("Predicting example {} loss: {}".format(example_i, loss.mean()))
      eval_loss.append(torch.flatten(loss))

      # Save rollout in testing
      if flags["mode"] == 'rollout':
        example_rollout['metadata'] = metadata
        filename = f'rollout_{example_i}.pkl'
        filename = os.path.join(flags["output_path"], filename)
        with open(filename, 'wb') as f:
          pickle.dump(example_rollout, f)

  print("Mean loss on rollout prediction: {}".format(
      torch.mean(torch.cat(eval_loss))))

def optimizer_to(optim, device):
  for param in optim.state.values():
    # Not sure there are any global tensors in the state dict
    if isinstance(param, torch.Tensor):
      param.data = param.data.to(device)
      if param._grad is not None:
        param._grad.data = param._grad.data.to(device)
    elif isinstance(param, dict):
      for subparam in param.values():
        if isinstance(subparam, torch.Tensor):
          subparam.data = subparam.data.to(device)
          if subparam._grad is not None:
            subparam._grad.data = subparam._grad.data.to(device)

def train(flags):
  """Train the model.

  Args:
    rank: local rank
    world_size: total number of ranks
  """   
  is_cuda = flags["is_cuda"]
  is_main = flags["is_main"]
  is_distributed = flags["is_distributed"]
  device = flags["device"]
  
  rank = flags["local_rank"]
  world_size = flags["world_size"]
  
  if is_cuda:
    logger = utils.init_logger(is_main=is_main, is_distributed=is_distributed, filename=f'{flags["log_path"]}run_{flags["exp_id"]}.log')
    logger.info(f"Main Proc on GPU {rank}.")
  else:
    logger = utils.init_logger(is_main=True, is_distributed=False, filename=f'{flags["log_path"]}run_{flags["exp_id"]}.log')
    logger.info(f"Running on CPU.")

  metadata = reading_utils.read_metadata(flags["data_path"])

  if is_cuda:
    serial_simulator = _get_simulator(metadata, flags["noise_std"], flags["noise_std"], device)
    simulator = DDP(serial_simulator.to(device), device_ids=[rank], output_device=device)
    optimizer = torch.optim.Adam(simulator.parameters(), lr=flags["lr_init"] * world_size)
  else:
    simulator = _get_simulator(metadata, flags["noise_std"], flags["noise_std"], device)
    optimizer = torch.optim.Adam(simulator.parameters(), lr=flags["lr_init"] * world_size)
  step = 0

  # If model_path does exist and model_file and train_state_file exist continue training.
  if flags["model_file"] is not None:

    if flags["model_file"] == "latest" and flags["train_state_file"] == "latest":
      # find the latest model, assumes model and train_state files are in step.
      fnames = glob.glob(f'{flags["model_path"]}{flags["exp_id"]}-model-*pt')
      max_model_number = 0
      expr = re.compile(f'.{flags["exp_id"]}-model-(\d+).pt')
      for fname in fnames:
        model_num = int(expr.search(fname).groups()[0])
        if model_num > max_model_number:
          max_model_number = model_num
      # reset names to point to the latest.
      flags["model_file"] = f'{flags["exp_id"]}-model-{max_model_number}.pt'
      flags["train_state_file"] = f"{flags['exp_id']}-train-state-{max_model_number}.pt"

    if os.path.exists(flags["model_path"] + flags["model_file"]) and os.path.exists(flags["model_path"] + flags["train_state_file"]):
      # load model
      if is_cuda:
        simulator.module.load(flags["model_path"] + flags["model_file"])
      else:
        simulator.load(flags["model_path"] + flags["model_file"])

      # load train state
      train_state = torch.load(flags["model_path"] + flags["train_state_file"])
      # set optimizer state
      if is_cuda:
        optimizer = torch.optim.Adam(simulator.module.parameters())
      else:
        optimizer = torch.optim.Adam(simulator.parameters())
      optimizer.load_state_dict(train_state["optimizer_state"])
      optimizer_to(optimizer, rank)
      # set global train state
      step = train_state["global_train_state"].pop("step")

    else:
      msg = f'Specified model_file {flags["model_path"] + flags["model_file"]} and train_state_file {flags["model_path"] + flags["train_state_file"]} not found.'
      # raise FileNotFoundError(msg)
      logger.info(msg)
      logger.info("Starting training from scratch.")

  simulator.train()
  simulator.to(device)

  if is_cuda:
    # dl = distribute.get_data_distributed_dataloader_SAG_Mill_baseline(path=flags["data_path"],
    #                                                                   input_length_sequence=INPUT_SEQUENCE_LENGTH,
    #                                                                   batch_size=flags["batch_size"],
    #                                                                   train_ratio=flags["train_ratio"],
    #                                                                   )
    dl = distribute.get_data_distributed_dataloader_mono_baseline(path=flags["data_path"],
                                                                      input_length_sequence=INPUT_SEQUENCE_LENGTH,
                                                                      batch_size=flags["batch_size"],
                                                                      train_ratio=flags["train_ratio"],
                                                                      )
  else:
    dl = data_loader.get_data_loader_SAG_Mill_baseline(path=flags["data_path"],
                                                input_length_sequence=INPUT_SEQUENCE_LENGTH,
                                                batch_size=flags["batch_size"],
                                                train_ratio=flags["train_ratio"],
                                                )

  print(f"rank = {rank}, cuda = {is_cuda}")
  not_reached_nsteps = True
  losses = []
  steps = []
  # all_acc = []
  try:
    while not_reached_nsteps:
      if is_cuda:
        torch.distributed.barrier()
      
      for ((position, particle_type, n_particles_per_example, edge_index), labels) in dl:
        position.to(device)
        particle_type.to(device)
        n_particles_per_example.to(device)
        edge_index.to(device)
        labels.to(device)

        # TODO (jpv): Move noise addition to data_loader
        # Sample the noise to add to the inputs to the model during training.
        sampled_noise = noise_utils.get_random_walk_noise_for_position_sequence(position, noise_std_last_step=flags["noise_std"]).to(device)
        non_kinematic_mask = (particle_type != KINEMATIC_PARTICLE_ID).clone().detach().to(device)
        sampled_noise *= non_kinematic_mask.view(-1, 1, 1)

        # Get the predictions and target accelerations.
        if is_cuda:
          pred_acc, target_acc = simulator.module.predict_accelerations(
              next_positions=labels.to(device),
              position_sequence_noise=sampled_noise.to(device),
              position_sequence=position.to(device),
              particle_types=particle_type.to(device),
              edge_index=edge_index.to(device))
          # all_acc.append(acc.cpu().detach().numpy())
        else:
          pred_acc, target_acc = simulator.predict_accelerations(
            next_positions=labels,
            position_sequence_noise=sampled_noise,
            position_sequence=position,
            particle_types=particle_type,
            edge_index=edge_index)

        # Calculate the loss and mask out loss on kinematic particles
        loss = (pred_acc - target_acc)**2
        loss = loss.sum(dim=-1)
        num_non_kinematic = non_kinematic_mask.sum()
        loss = torch.where(non_kinematic_mask.bool(),
                         loss, torch.zeros_like(loss))
        loss = loss.sum() / num_non_kinematic

        # Computes the gradient of loss
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # Update learning rate
        lr_new = flags["lr_init"] * (flags["lr_decay"] ** (step/flags["lr_decay_steps"])) * world_size
        for param in optimizer.param_groups:
          param['lr'] = lr_new

        
        if step % flags["nlog_steps"] == 0:
          if is_cuda:
            torch.distributed.reduce(loss, dst=0, op=torch.distributed.ReduceOp.SUM)
          logger.info(f'Training step: {step}/{flags["ntraining_steps"]}. Loss: {loss / world_size}.')
          losses.append((loss / world_size).cpu().detach().numpy())
          steps.append(step)
            
        if is_main:
          # Save model state
          if step % flags["nsave_steps"] == 0:
            if not is_cuda:
              simulator.save(flags["model_path"] + f'{flags["exp_id"]}-model-'+str(step)+'.pt')
            else:
              simulator.module.save(flags["model_path"] + f'{flags["exp_id"]}-model-'+str(step)+'.pt')
            train_state = dict(optimizer_state=optimizer.state_dict(), global_train_state={"step":step})
            torch.save(train_state, f'{flags["model_path"]}{flags["exp_id"]}-train-state-{step}.pt')

        # Complete training
        if (step >= flags["ntraining_steps"]):
          not_reached_nsteps = False
          break

        step += 1
        
    import matplotlib.pyplot as plt
    plt.plot(steps, losses)
    plt.xlabel("steps")
    plt.ylabel("loss")
    plt.savefig(f'{flags["model_path"]}{flags["exp_id"]}-loss.png')
    
    # all_acc = np.concatenate(all_acc, axis=0)
    # pickle.dump(all_acc, open(f'{flags["model_path"]}{flags["exp_id"]}-acc.pkl', 'wb'))

  except KeyboardInterrupt:
    pass

  # if is_main:
  #   if not is_cuda:
  #     simulator.save(flags["model_path"] + 'model-'+str(step)+'.pt')
  #   else:
  #     simulator.module.save(flags["model_path"] + 'model-'+str(step)+'.pt')
  #   train_state = dict(optimizer_state=optimizer.state_dict(), global_train_state={"step":step})
  #   torch.save(train_state, f'{flags["model_path"]}train_state-{step}.pt')

  if is_cuda:
    distribute.cleanup()


def _get_simulator(
        metadata: json,
        acc_noise_std: float,
        vel_noise_std: float,
        device: str) -> learned_simulator.LearnedSimulator:
  """Instantiates the simulator.

  Args:
    metadata: JSON object with metadata.
    acc_noise_std: Acceleration noise std deviation.
    vel_noise_std: Velocity noise std deviation.
    device: PyTorch device 'cpu' or 'cuda'.
  """

  # Normalization stats
  normalization_stats = {
      'acceleration': {
          'mean': torch.FloatTensor(metadata['acc_mean']).to(device),
          'std': torch.sqrt(torch.FloatTensor(metadata['acc_std'])**2 +
                            acc_noise_std**2).to(device),
      },
      'velocity': {
          'mean': torch.FloatTensor(metadata['vel_mean']).to(device),
          'std': torch.sqrt(torch.FloatTensor(metadata['vel_std'])**2 +
                            vel_noise_std**2).to(device),
      },
  }

  # simulator = learned_simulator.LearnedSimulator(
  #     particle_dimensions=metadata['dim'],
  #     nnode_in=37 if metadata['dim'] == 3 else 30,
  #     nedge_in=metadata['dim'] + 1,
  #     latent_dim=128,
  #     nmessage_passing_steps=5,
  #     nmlp_layers=2,
  #     mlp_hidden_dim=128,
  #     boundaries=np.array(metadata['bounds']),
  #     normalization_stats=normalization_stats,
  #     nparticle_types=NUM_PARTICLE_TYPES,
  #     particle_type_embedding_size=16,
  #     device=device)
  cylinder = learned_simulator.Cylinder(
                metadata['geometry']['axis_start'], 
                metadata['geometry']['axis_end'], 
                metadata['geometry']['radius']
              )
  
  simulator = learned_simulator.LearnedCylinderSimulator(
      particle_dimensions=metadata['dim'],
      nnode_in=18 if metadata['dim'] == 3 else 30,
      nedge_in=metadata['dim'] + 1,
      latent_dim=128,
      nmessage_passing_steps=5,
      nmlp_layers=2,
      mlp_hidden_dim=128,
      cylinder=cylinder,
      radius=metadata['geometry']['radius'],
      dt=metadata['dt'],
      normalization_stats=normalization_stats,
      nparticle_types=NUM_PARTICLE_TYPES,
      particle_type_embedding_size=16,
      device=device)

  return simulator


def main():
  """Train or evaluates the model.

  """
  myflags = {}
  myflags["data_path"] = args.data_path
  myflags["noise_std"] = args.noise_std
  myflags["lr_init"] = args.lr_init
  myflags["lr_decay"] = args.lr_decay
  myflags["lr_decay_steps"] = args.lr_decay_steps
  myflags["batch_size"] = args.batch_size
  myflags["ntraining_steps"] = args.ntraining_steps
  myflags["nvalid_steps"] = args.nvalid_steps
  myflags["nsave_steps"] = args.nsave_steps
  myflags["model_file"] = args.model_file
  myflags["model_path"] = args.model_path
  myflags["train_state_file"] = args.train_state_file
  myflags["mode"] = args.mode
  myflags["output_path"] = args.output_path
  myflags["exp_id"] = args.exp_id
  myflags["nlog_steps"] = args.nlog_steps
  myflags["is_cuda"] = args.is_cuda
  myflags["log_path"] = args.log_path
  myflags["train_ratio"] = 0.8
  myflags = utils.init_distritubed_mode(myflags)

  # Read metadata
  if args.mode == 'train':
    # If model_path does not exist create new directory.
    if not os.path.exists(myflags["model_path"]):
      os.makedirs(myflags["model_path"])
    if myflags["is_cuda"]:
      torch.distributed.barrier()
    train(myflags)

  elif args.mode in ['valid', 'rollout']:
    if myflags["is_cuda"]:
      torch.distributed.barrier()
    predict(myflags)
  
  # elif args.mode in ['rollout']:
  #   if myflags["is_cuda"]:
  #     torch.distributed.barrier()
  #   data = np.load('/mnt/raid0sata1/jysc/gnn_data/Train_Data/all_pos.npz')
  #   device = myflags["device"]
  #   metadata = reading_utils.read_metadata(myflags["data_path"])
  #   simulator = _get_simulator(metadata, myflags["noise_std"], myflags["noise_std"], device)

  #   # Load simulator
  #   if os.path.exists(myflags["model_path"] + myflags["model_file"]):
  #     simulator.load(myflags["model_path"] + myflags["model_file"])
  #   else:
  #     raise FileNotFoundError(f'Model file {myflags["model_path"] + myflags["model_file"]} not found.')
  #   simulator.to(device)
  #   simulator.eval()

  #   # Output path
  #   if not os.path.exists(myflags["output_path"]):
  #     os.makedirs(myflags["output_path"])
    
  #   output_dict, _ = rollout(simulator, 
  #           torch.tensor(data['all_pos']).to(torch.float32).contiguous(), 
  #           torch.full((data['all_pos'].shape[0],), 0, dtype=int).contiguous(), 
  #           data['all_pos'].shape[0], 
  #           metadata['sequence_length'] - INPUT_SEQUENCE_LENGTH, 
  #           device)
    
  #   pickle.dump(output_dict, open(f'{myflags["output_path"]}rollout.pkl', 'wb'))
    

if __name__ == '__main__':
  main()