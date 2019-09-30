# this is from FFJORD
import torch
import torch.nn as nn
from torch.autograd import Variable
from code.vae_lib.models.train_misc import build_model_tabular
from .VAE import VAE
from code.vae_lib.models.model import ProductOfExperts, prior_expert


def get_hidden_dims(args):
    return tuple(map(int, args.dims.split("-"))) + (args.z_size,)


def concat_layer_num_params(in_dim, out_dim):
    return (in_dim + 1) * out_dim + out_dim


def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=1, bias=False)


class GenCNFVAE(VAE):

    def __init__(self, args, encoders, decoders):
        super(GenCNFVAE, self).__init__(args)
        print("CNF created")
        self.experts = ProductOfExperts()
        # CNF model
        self.cnf = build_model_tabular(args, args.z_size)
        self.encoders = nn.ModuleList()
        for enc in encoders:
            self.encoders.append(enc(args.z_size))
        self.decoders = nn.ModuleList()
        for dec in decoders:
            self.decoders.append(dec(args.z_size))
        self.z_size = args.z_size

    def forward(self, inputs):
        """
        Forward pass with planar flows for the transformation z_0 -> z_1 -> ... -> z_k.
        Log determinant is computed as log_det_j = N E_q_z0[\sum_k log |det dz_k/dz_k-1| ].
        """
        z_mu, z_var = self.encode(inputs)
        # Sample z_0
        z0 = self.reparameterize(z_mu, z_var)
        # z0 = z0.to(z_mu)
        zero = torch.zeros(z0.shape[0], 1).to(z0)

        zk, delta_logp = self.cnf(z0, zero)  # run model forward

        reconstructions = []
        for dec in self.decoders:
            reconstructions.append(dec(zk))

        return reconstructions, z_mu, z_var, -delta_logp.view(-1), z0, zk

    def encode(self, inputs):
        """
        Encoder that ouputs parameters for base distribution of z and flow parameters.
        """

        mean_z, var_z = self.infer(inputs)

        return mean_z, var_z

    def infer(self, inputs):

        batch_size = 1
        for inp in inputs:
            if inp is not None:
                batch_size = inp.size(0)

        use_cuda = next(self.parameters()).is_cuda  # check if CUDA
        # initialize the universal prior expert
        mu, logvar = prior_expert((1, batch_size, self.z_size),
                                  use_cuda=use_cuda)

        for inp, enc in zip(inputs, self.encoders):
            if inp is None:
                continue
            mean_z, var_z = enc(inp)
            mu = torch.cat((mu, mean_z.unsqueeze(0)), dim=0)
            logvar = torch.cat((logvar, var_z.unsqueeze(0)), dim=0)

        # product of experts to combine gaussians

        mu, logvar = self.experts(mu, logvar)

        return mu, logvar

    def reparameterize(self, mu, var):
        """
        Samples z from a multivariate Gaussian with diagonal covariance matrix using the
         reparameterization trick.
        """
        if self.training:
            std = var.mul(0.5).exp_()
            eps = Variable(std.data.new(std.size()).normal_())
            return eps.mul(std).add_(mu)
        else:
            return mu
