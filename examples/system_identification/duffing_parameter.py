"""
Parameter estimation of Duffing ODE system using SGD
"""

import torch
import numpy as np
import slim
import psl
import os

from neuromancer import blocks, estimators, dynamics, integrators, ode
from neuromancer.interpolation import LinInterp_Offline
from neuromancer.visuals import VisualizerOpen
from neuromancer.trainer import Trainer
from neuromancer.problem import Problem
from neuromancer.simulators import OpenLoopSimulator, MultiSequenceOpenLoopSimulator
from neuromancer.callbacks import SysIDCallback
from neuromancer.loggers import BasicLogger, MLFlowLogger
from neuromancer.constraint import variable
from neuromancer.dataset import get_sequence_dataloaders
from neuromancer.loss import PenaltyLoss


torch.manual_seed(0)
# %%
device = "cpu"

# %%
system = psl.systems['Duffing'] # non-autonomous system

ts = 0.01
nsim = 1000
modelSystem = system(ts=ts, nsim=nsim)
raw = modelSystem.simulate()
psl.plot.pltOL(Y=raw['Y'])
psl.plot.pltPhase(X=raw['Y'])

t = (np.arange(nsim)*ts).reshape(-1, 1)
raw['Time'] = t

t = torch.from_numpy(t)
interp_u = LinInterp_Offline(t, t)

nsteps = 2
nstep_data, loop_data, dims = get_sequence_dataloaders(raw, nsteps,
 moving_horizon=True)

train_data, dev_data, test_data = nstep_data    #(nstep, # batches, sys dim)
train_loop, dev_loop, test_loop = loop_data

# %% Identity mapping
nx = dims['X'][1]

estim = estimators.FullyObservable(
    {**train_data.dataset.dims, "x0": (nx,)},
    linear_map = slim.maps['identity'],
    input_keys = ["Yp"],
)

estim(train_data.dataset.get_full_batch())

# %% Instantiate the blocks, dynamics model:
duffing_sys = ode.DuffingParam()

fx_int = integrators.RK4(duffing_sys, interp_u=interp_u, h=modelSystem.ts)

fy = slim.maps['identity'](nx, nx)

dynamics_model = dynamics.ODENonAuto(fx_int, fy, 
input_key_map={"x0": f"x0_{estim.name}", "Time": "Timef", 'Yf': 'Yf'},  # TBC2: sth wrong with input_key_map
name='dynamics',    # must be named 'dynamics' due to some issue in visuals.py
online_flag=False
)
# %% Constraints + losses:
yhat = variable(f"Y_pred_{dynamics_model.name}")
y = variable("Yf")

yFD = (y[:, 1:, :] - y[:, :-1, :])
yhatFD = (yhat[:, 1:, :] - yhat[:, :-1, :])

fd_loss = 2.0*((yFD == yhatFD)^2)
fd_loss.name = 'FD_loss'

reference_loss = ((yhat == y)^2)
reference_loss.name = "ref_loss"

# %%
objectives = [reference_loss, fd_loss]
constraints = []
components = [estim, dynamics_model]
# create constrained optimization loss
loss = PenaltyLoss(objectives, constraints)
# construct constrained optimization problem
problem = Problem(components, loss)
# plot computational graph
problem.plot_graph()
problem = problem.to(device)

# %%
optimizer = torch.optim.Adam(problem.parameters(), lr=0.1)
logger = BasicLogger(args=None, savedir='test', verbosity=1,
                     stdout="nstep_dev_"+reference_loss.output_keys[0])

simulator = OpenLoopSimulator(
    problem, train_loop, dev_loop, test_loop, eval_sim=True, device=device,
) if isinstance(train_loop, dict) else MultiSequenceOpenLoopSimulator(
    problem, train_loop, dev_loop, test_loop, eval_sim=True, device=device,
)
visualizer = VisualizerOpen(
    dynamics_model,
    1,
    'test',
    training_visuals=False,
    trace_movie=False,
)
callback = SysIDCallback(simulator, visualizer)

trainer = Trainer(
    problem,
    train_data,
    dev_data,
    test_data,
    optimizer,
    callback=callback,
    patience=10,
    warmup=10,
    epochs=100,
    eval_metric="nstep_dev_"+reference_loss.output_keys[0],
    train_metric="nstep_train_loss",
    dev_metric="nstep_dev_loss",
    test_metric="nstep_test_loss",
    logger=logger,
    device=device,
)
best_model = trainer.train()
best_outputs = trainer.test(best_model)
os.system('cp test/open_loop.png ../figs/duffing_parameter.png')
