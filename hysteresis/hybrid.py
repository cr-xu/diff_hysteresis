from typing import Any, List, Union

import torch
from botorch.models import SingleTaskGP
from botorch.models.gpytorch import GPyTorchModel
from botorch.models.transforms import Normalize, Standardize
from botorch.posteriors import GPyTorchPosterior
from gpytorch.models import GP
from torch import Tensor

from hysteresis.base import BaseHysteresis, HysteresisError
from hysteresis.modes import FITTING, NEXT, ModeModule


class ExactHybridGP(ModeModule, GP, GPyTorchModel):
    num_outputs = 1

    def __init__(
        self,
        train_x: Tensor,
        train_y: Tensor,
        hysteresis_models: List[BaseHysteresis] | BaseHysteresis,
        **kwargs
    ):
        """
        Joint hysteresis - Gaussian process module used to fit beam response data
        when hysteresis effects are present.

        The model works as follows:
        hysteresis_input -> hysteresis_output -> normalization -> GP_model_input ->
        GP_model_output

        This model uses the same mode convention as hysteresis.base.BaseHysteresis
        that controls the output of the forward() method for training, prediction etc.
        Model must be in NEXT mode for use in Botorch acquisition functions.

        From this model we are able to infer hysteresis parameters up to a scale +
        offset factor.

        Parameters
        ----------
        train_x : Tensor
            Sequence of input training data (input current or magnetization). Shape
            must be N x M where M is equal to the number of hysteresis models passed
            to this constructor.

        train_y :
            Sequence of output training data (beam measurements etc.). Tensor shape
            must be (N,)

        hysteresis_models: List[BaseHysteresis]
            List of M independent hysteresis models to model each element exibiting
            hysteresis.

        kwargs
            Arguments passed to botorch SingleTaskGP object.
        """

        super(ExactHybridGP, self).__init__()

        if train_x.shape[0] != train_y.shape[0]:
            raise ValueError("train_x and train_y must have the same number of samples")

        if len(train_y.shape) != 1:
            raise ValueError(
                "multi output models are not supported, train_y must be a 1D tensor"
            )

        if not isinstance(hysteresis_models, list):
            self.hysteresis_models = torch.nn.ModuleList([hysteresis_models])
        else:
            self.hysteresis_models = torch.nn.ModuleList(hysteresis_models)

        # check if all elements are unique
        if not (len(set(self.hysteresis_models)) == len(self.hysteresis_models)):
            raise ValueError("all hysteresis models must be unique")

        # check that training.py data is the correct size
        self.input_dim = train_x.shape[-1]
        if self.input_dim != len(self.hysteresis_models):
            raise ValueError(
                "training.py data must match the number of hysteresis models"
            )

        # set hysteresis model history data
        self._set_hysteresis_model_train_data(train_x)

        # set train inputs
        self.train_inputs = (train_x,)

        self.m_transform = Normalize(self.input_dim)
        # Make sure the transformation have the same dtype
        self.m_transform._to(train_x)
        self.untrained_transform = Normalize(self.input_dim)
        self.untrained_transform._to(train_x)

        # train outcome transform
        self.outcome_transform = Standardize(1)
        self.outcome_transform.train()
        self.train_targets = self.outcome_transform(train_y.unsqueeze(1))[0].flatten()
        self.outcome_transform.eval()

        # get magnetization from hysteresis models
        train_m = self.get_magnetization(train_x, mode=FITTING).detach()

        self.gp = SingleTaskGP(train_m, train_y.unsqueeze(1), **kwargs)

    def __call__(self, *inputs, **kwargs):
        return self.forward(*inputs, **kwargs)

    def _set_hysteresis_model_train_data(self, train_h):
        for idx, hyst_model in enumerate(self.hysteresis_models):
            hyst_model.set_history(train_h[:, idx])

    def apply_fields(self, x: Tensor):
        for idx, hyst_model in enumerate(self.hysteresis_models):
            hyst_model.apply_field(x[:, idx])

    def get_magnetization(self, X, mode=None):
        train_m = []
        # set applied fields and calculate magnetization for training.py data
        for idx, hyst_model in enumerate(self.hysteresis_models):
            hyst_model.mode = mode or self.mode
            train_m += [hyst_model(X[..., idx], return_real=True)]
        return torch.cat([ele.unsqueeze(-1) for ele in train_m], dim=-1)

    def get_normalized_magnetization(self, X, mode=None):
        m = self.get_magnetization(X, mode)

        # check to see if a normalization model has been trained
        if not self.m_transform.equals(self.untrained_transform) or self.training:
            return self.m_transform(m)
        else:
            return m

    def posterior(
        self, X: Tensor, observation_noise: Union[bool, Tensor] = False, **kwargs: Any
    ) -> GPyTorchPosterior:
        if self.mode != NEXT:
            raise HysteresisError("calling posterior requires NEXT mode")
        M = self.get_normalized_magnetization(X)

        return self.gp.posterior(
            M.double(), observation_noise=observation_noise, **kwargs
        )

    def forward(
        self, X, from_magnetization=False, return_real=False, return_likelihood=False
    ):
        train_m = self.get_normalized_magnetization(X)

        if self.training:
            self.gp.set_train_data(train_m, self.train_targets)

        if return_likelihood and return_real:
            lk = self.gp.likelihood(self.gp(train_m.unsqueeze(-1)))
            return self.outcome_transform.untransform_posterior(lk)

        elif return_real:
            return self.outcome_transform.untransform_posterior(
                self.gp(train_m.unsqueeze(-1))
            )
        else:
            return self.gp(train_m)
