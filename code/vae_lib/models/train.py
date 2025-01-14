#train_mvae


from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import os
import sys
import shutil

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable
from torchvision import transforms
from torchvision.datasets import MNIST

import code.lib.utils as utils
import code.lib.layers.odefunc as odefunc

import code.vae_lib.models.VAE as VAE
import code.vae_lib.models.CNFVAE as CNFVAE
from code.vae_lib.optimization.training import train, evaluate
from code.vae_lib.utils.load_data import load_dataset
from code.vae_lib.utils.plotting import plot_training_curve

#from code.vae_lib.models.model import MVAE


def elbo_loss(recon_image, image, recon_text, text, mu, logvar,
              lambda_image=1.0, lambda_text=1.0, annealing_factor=1):
    """Bimodal ELBO loss function. 
    
    @param recon_image: torch.Tensor
                        reconstructed image
    @param image: torch.Tensor
                  input image
    @param recon_text: torch.Tensor
                       reconstructed text probabilities
    @param text: torch.Tensor
                 input text (one-hot)
    @param mu: torch.Tensor
               mean of latent distribution
    @param logvar: torch.Tensor
                   log-variance of latent distribution
    @param lambda_image: float [default: 1.0]
                         weight for image BCE
    @param lambda_text: float [default: 1.0]
                       weight for text BCE
    @param annealing_factor: integer [default: 1]
                             multiplier for KL divergence term
    @return ELBO: torch.Tensor
                  evidence lower bound
    """
    image_bce, text_bce = 0, 0  # default params
    if recon_image is not None and image is not None:
        image_bce = torch.sum(binary_cross_entropy_with_logits(
            recon_image.view(-1, 1 * 28 * 28), 
            image.view(-1, 1 * 28 * 28)), dim=1)

    if recon_text is not None and text is not None:
        text_bce = torch.sum(cross_entropy(recon_text, text), dim=1)

    # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
    # https://arxiv.org/abs/1312.6114
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp(), dim=1)
    ELBO = torch.mean(lambda_image * image_bce + lambda_text * text_bce 
                      + annealing_factor * KLD)
    return ELBO


def binary_cross_entropy_with_logits(input, target):
    """Sigmoid Activation + Binary Cross Entropy

    @param input: torch.Tensor (size N)
    @param target: torch.Tensor (size N)
    @return loss: torch.Tensor (size N)
    """
    if not (target.size() == input.size()):
        raise ValueError("Target size ({}) must be the same as input size ({})".format(
            target.size(), input.size()))

    return (torch.clamp(input, 0) - input * target 
            + torch.log(1 + torch.exp(-torch.abs(input))))


def cross_entropy(input, target, eps=1e-6):
    """k-Class Cross Entropy (Log Softmax + Log Loss)
    
    @param input: torch.Tensor (size N x K)
    @param target: torch.Tensor (size N x K)
    @param eps: error to add (default: 1e-6)
    @return loss: torch.Tensor (size N)
    """
    if not (target.size(0) == input.size(0)):
        raise ValueError(
            "Target size ({}) must be the same as input size ({})".format(
                target.size(0), input.size(0)))

    log_input = F.log_softmax(input + eps, dim=1)
    y_onehot = Variable(log_input.data.new(log_input.size()).zero_())
    y_onehot = y_onehot.scatter(1, target.unsqueeze(1), 1)
    loss = y_onehot * log_input
    return -loss


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def save_checkpoint(state, is_best, folder='./', filename='checkpoint.pth.tar'):
    if not os.path.isdir(folder):
        os.mkdir(folder)
    torch.save(state, os.path.join(folder, filename))
    if is_best:
        shutil.copyfile(os.path.join(folder, filename),
                        os.path.join(folder, 'model_best.pth.tar'))


def load_checkpoint(file_path, use_cuda=False):
    checkpoint = torch.load(file_path) if use_cuda else \
        torch.load(file_path, map_location=lambda storage, location: storage)

    model = CNFVAE.CNFVAE(checkpoint['args'])
    model.load_state_dict(checkpoint['state_dict'])
    return model


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--n-latents', type=int, default=64,
                        help='size of the latent embedding [default: 64]')
    parser.add_argument('--batch-size', type=int, default=256, metavar='N',
                        help='input batch size for training [default: 100]')
    parser.add_argument('--epochs', type=int, default=100, metavar='N',
                        help='number of epochs to train [default: 500]')
    parser.add_argument('--annealing-epochs', type=int, default=200, metavar='N',
                        help='number of epochs to anneal KL for [default: 200]')
    parser.add_argument('--lr', type=float, default=1e-3, metavar='LR',
                        help='learning rate [default: 1e-3]')
    parser.add_argument('--log-interval', type=int, default=10, metavar='N',
                        help='how many batches to wait before logging training status [default: 10]')
    parser.add_argument('--lambda-image', type=float, default=1.,
                        help='multipler for image reconstruction [default: 1]')
    parser.add_argument('--lambda-text', type=float, default=10.,
                        help='multipler for text reconstruction [default: 10]')
    parser.add_argument('--cuda', action='store_true', default=True,
                        help='enables CUDA training [default: True]')

    SOLVERS = ["dopri5", "bdf", "rk4", "midpoint", 'adams', 'explicit_adams', 'fixed_adams']


    parser.add_argument(
        '-d', '--dataset', type=str, default='mnist', choices=['mnist', 'freyfaces', 'omniglot', 'caltech'],
        metavar='DATASET', help='Dataset choice.'
    )

    parser.add_argument(
        '-freys', '--freyseed', type=int, default=123, metavar='FREYSEED',
        help="""Seed for shuffling frey face dataset for test split. Ignored for other datasets.
                        Results in paper are produced with seeds 123, 321, 231"""
    )

    parser.add_argument('-nc', '--no_cuda', action='store_true', default=False, help='disables CUDA training')

    parser.add_argument('--manual_seed', type=int, help='manual seed, if not given resorts to random seed.')

    parser.add_argument(
        '-li', '--log_interval', type=int, default=10, metavar='LOG_INTERVAL',
        help='how many batches to wait before logging training status'
    )

    parser.add_argument(
        '-od', '--out_dir', type=str, default='snapshots', metavar='OUT_DIR',
        help='output directory for model snapshots etc.'
    )

    # optimization settings
    parser.add_argument(
        '-es', '--early_stopping_epochs', type=int, default=35, metavar='EARLY_STOPPING',
        help='number of early stopping epochs'
    )

    parser.add_argument(
        '-bs', '--batch_size', type=int, default=256, metavar='BATCH_SIZE', help='input batch size for training'
    )
    parser.add_argument('-lr', '--learning_rate', type=float, default=0.0005, metavar='LEARNING_RATE',
                        help='learning rate')

    parser.add_argument(
        '-w', '--warmup', type=int, default=100, metavar='N',
        help='number of epochs for warm-up. Set to 0 to turn warmup off.'
    )
    parser.add_argument('--max_beta', type=float, default=1., metavar='MB', help='max beta for warm-up')
    parser.add_argument('--min_beta', type=float, default=0.0, metavar='MB', help='min beta for warm-up')
    parser.add_argument(
        '-f', '--flow', type=str, default='cnf', choices=[
            'planar', 'iaf', 'householder', 'orthogonal', 'triangular', 'cnf', 'cnf_bias', 'cnf_hyper', 'cnf_rank',
            'cnf_lyper', 'no_flow'
        ], help="""Type of flows to use, no flows can also be selected"""
    )
    parser.add_argument('-r', '--rank', type=int, default=1)
    parser.add_argument(
        '-nf', '--num_flows', type=int, default=4, metavar='NUM_FLOWS',
        help='Number of flow layers, ignored in absence of flows'
    )
    parser.add_argument(
        '-nv', '--num_ortho_vecs', type=int, default=8, metavar='NUM_ORTHO_VECS',
        help=""" For orthogonal flow: How orthogonal vectors per flow do you need.
                        Ignored for other flow types."""
    )
    parser.add_argument(
        '-nh', '--num_householder', type=int, default=8, metavar='NUM_HOUSEHOLDERS',
        help=""" For Householder Sylvester flow: Number of Householder matrices per flow.
                        Ignored for other flow types."""
    )
    parser.add_argument(
        '-mhs', '--made_h_size', type=int, default=320, metavar='MADEHSIZE',
        help='Width of mades for iaf. Ignored for all other flows.'
    )
    parser.add_argument('--z_size', type=int, default=64, metavar='ZSIZE', help='how many stochastic hidden units')
    # gpu/cpu
    parser.add_argument('--gpu_num', type=int, default=0, metavar='GPU', help='choose GPU to run on.')

    # CNF settings
    parser.add_argument(
        "--layer_type", type=str, default="concat",
        choices=["ignore", "concat", "concat_v2", "squash", "concatsquash", "concatcoord", "hyper", "blend"]
    )
    parser.add_argument('--dims', type=str, default='512-512')
    parser.add_argument("--num_blocks", type=int, default=1, help='Number of stacked CNFs.')
    parser.add_argument('--time_length', type=float, default=0.5)
    parser.add_argument('--train_T', type=eval, default=False)
    parser.add_argument("--divergence_fn", type=str, default="approximate", choices=["brute_force", "approximate"])
    parser.add_argument("--nonlinearity", type=str, default="softplus", choices=odefunc.NONLINEARITIES)

    parser.add_argument('--solver', type=str, default='dopri5', choices=SOLVERS)
    parser.add_argument('--atol', type=float, default=1e-5)
    parser.add_argument('--rtol', type=float, default=1e-5)
    parser.add_argument("--step_size", type=float, default=None, help="Optional fixed step size.")

    parser.add_argument('--test_solver', type=str, default=None, choices=SOLVERS + [None])
    parser.add_argument('--test_atol', type=float, default=None)
    parser.add_argument('--test_rtol', type=float, default=None)

    parser.add_argument('--residual', type=eval, default=False, choices=[True, False])
    parser.add_argument('--rademacher', type=eval, default=False, choices=[True, False])
    parser.add_argument('--batch_norm', type=eval, default=False, choices=[True, False])
    parser.add_argument('--bn_lag', type=float, default=0)
    # evaluation
    parser.add_argument('--evaluate', type=eval, default=False, choices=[True, False])
    parser.add_argument('--model_path', type=str, default='')
    parser.add_argument('--retrain_encoder', type=eval, default=False, choices=[True, False])


    args = parser.parse_args()
    args.cuda = args.cuda and torch.cuda.is_available()
    if not os.path.isdir('./trained_models'):
        os.makedirs('./trained_models')

    train_loader   = torch.utils.data.DataLoader(
        MNIST('./data', train=True, download=True, transform=transforms.ToTensor()),
        batch_size=args.batch_size, shuffle=True)
    N_mini_batches = len(train_loader)
    test_loader    = torch.utils.data.DataLoader(
        MNIST('./data', train=False, download=True, transform=transforms.ToTensor()),
        batch_size=args.batch_size, shuffle=False)

    model     = MVAE(args.n_latents)
    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    if args.cuda:
        model.cuda()


    def train(epoch):
        model.train()
        train_loss_meter = AverageMeter()

        # NOTE: is_paired is 1 if the example is paired
        for batch_idx, (image, text) in enumerate(train_loader):
            if epoch < args.annealing_epochs:
                # compute the KL annealing factor for the current mini-batch in the current epoch
                annealing_factor = (float(batch_idx + (epoch - 1) * N_mini_batches + 1) /
                                    float(args.annealing_epochs * N_mini_batches))
            else:
                # by default the KL annealing factor is unity
                annealing_factor = 1.0

            if args.cuda:
                image  = image.cuda()
                text   = text.cuda()

            image      = Variable(image)
            text       = Variable(text)
            batch_size = len(image)

            # refresh the optimizer
            optimizer.zero_grad()

            # pass data through model
            recon_image_1, recon_text_1, mu_1, logvar_1 = model(image, text)
            recon_image_2, recon_text_2, mu_2, logvar_2 = model(image)
            recon_image_3, recon_text_3, mu_3, logvar_3 = model(text=text)
                
            # compute ELBO for each data combo
            joint_loss = elbo_loss(recon_image_1, image, recon_text_1, text, mu_1, logvar_1, 
                                   lambda_image=args.lambda_image, lambda_text=args.lambda_text,
                                   annealing_factor=annealing_factor)
            image_loss = elbo_loss(recon_image_2, image, None, None, mu_2, logvar_2, 
                                   lambda_image=args.lambda_image, lambda_text=args.lambda_text,
                                   annealing_factor=annealing_factor)
            text_loss  = elbo_loss(None, None, recon_text_3, text, mu_3, logvar_3, 
                                   lambda_image=args.lambda_image, lambda_text=args.lambda_text,
                                  annealing_factor=annealing_factor)
            train_loss = joint_loss + image_loss + text_loss
            train_loss_meter.update(train_loss.item(), batch_size)
            
            # compute gradients and take step
            train_loss.backward()
            optimizer.step()

            if batch_idx % args.log_interval == 0:
                print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}\tAnnealing-Factor: {:.3f}'.format(
                    epoch, batch_idx * len(image), len(train_loader.dataset),
                    100. * batch_idx / len(train_loader), train_loss_meter.avg, annealing_factor))

        print('====> Epoch: {}\tLoss: {:.4f}'.format(epoch, train_loss_meter.avg))


    def test(epoch):
        model.eval()
        test_loss_meter = AverageMeter()

        for batch_idx, (image, text) in enumerate(test_loader):
            if args.cuda:
                image  = image.cuda()
                text   = text.cuda()

            image = Variable(image, volatile=True)
            text  = Variable(text, volatile=True)
            batch_size = len(image)

            recon_image_1, recon_text_1, mu_1, logvar_1 = model(image, text)
            recon_image_2, recon_text_2, mu_2, logvar_2 = model(image)
            recon_image_3, recon_text_3, mu_3, logvar_3 = model(text=text)

            joint_loss = elbo_loss(recon_image_1, image, recon_text_1, text, mu_1, logvar_1)
            image_loss = elbo_loss(recon_image_2, image, None, None, mu_2, logvar_2)
            text_loss  = elbo_loss(None, None, recon_text_3, text, mu_3, logvar_3)
            test_loss  = joint_loss + image_loss + text_loss
            test_loss_meter.update(test_loss.item(), batch_size)

        print('====> Test Loss: {:.4f}'.format(test_loss_meter.avg))
        return test_loss_meter.avg

    
    best_loss = float("inf")
    for epoch in range(1, args.epochs + 1):
        train(epoch)
        test_loss = test(epoch)
        is_best   = test_loss < best_loss
        best_loss = min(test_loss, best_loss)
        # save the best model and current model
        save_checkpoint({
            'state_dict': model.state_dict(),
            'best_loss': best_loss,
            'n_latents': args.n_latents,
            'optimizer' : optimizer.state_dict(),
        }, is_best, folder='./trained_models')   
