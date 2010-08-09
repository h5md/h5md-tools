# -*- coding: utf-8 -*-
#
# mdplot - Molecular Dynamics simulation plotter
#
# Copyright © 2008-2010  Peter Colberg, Felix Höfling
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA.
#

import os, os.path
from matplotlib import ticker
from numpy import *
from re import split
import tables
import mdplot.label
import ssf
from mdplot.ext import _static_structure_factor

import pycuda.autoinit
import pycuda.driver as cuda

"""
Compute and plot static structure factor
"""
def plot(args):
    from matplotlib import pyplot as plt

    if args.cuda:
        make_cuda_kernels()

    ax = plt.axes()
    label = None
    ax.axhline(y=1, color='black', lw=0.5)
    ax.set_color_cycle(args.colors)

    for (i, fn) in enumerate(args.input):
        try:
            f = tables.openFile(fn, mode='r')
        except IOError:
            raise SystemExit('failed to open HDF5 file: %s' % fn)

        H5 = f.root
        param = H5.param
        try:
            if args.flavour:
                trajectory = H5.trajectory._v_children[args.flavour]
            else:
                trajectory = H5.trajectory

            # periodically extended particle positions
            # possibly read several samples
            idx = [int(x) for x in split(':', args.sample)]
            if len(idx) == 1 :
                samples = array([trajectory.r[idx[0]]])
            elif len(idx) == 2:
                samples = trajectory.r[idx[0]:idx[1]]
            # periodic simulation box length
            L = param.mdsim._v_attrs.box_length
            # number of particles
            N = sum(param.mdsim._v_attrs.particles)
            # positional coordinates dimension
            dim = param.mdsim._v_attrs.dimension

            # store attributes for later use before closing the file
            attrs = mdplot.label.attributes(param)

        except IndexError:
            raise SystemExit('invalid phase space sample offset')
        except tables.exceptions.NoSuchNodeError as what:
            raise SystemExit(str(what) + '\nmissing simulation data in file: %s' % fn)
        finally:
            f.close()

        # reciprocal lattice distance
        q_min = (2 * pi / L)
        # number of values for |q|
        nq = int(args.q_limit / q_min)
        # absolute deviation of |q|
        q_err = q_min * args.q_error

        # generate n-dimensional q-grid
        q_grid = q_min * squeeze(dstack(reshape(indices(repeat(nq + 1, dim)), (dim, -1))))
        # compute absolute |q| values of q-grid
        q_norm = sqrt(sum(q_grid * q_grid, axis=1))

        # |q| value range
        q_range = q_min * arange(1, nq + 1)

        # compute static structure factor over |q| range
        from time import time
        S_q = zeros(nq)
        S_q2 = zeros(nq)
        timer_host = 0
        timer_gpu = 0
        for j, q_val in enumerate(q_range):
            # choose q vectors on surface of Ewald's sphere
            q = q_grid[where(abs(q_norm - q_val) < q_err)]
            if args.verbose:
                print '|q| = %.2f\t%4d vectors' % (q_val, len(q))
            # average static structure factor over q vectors
            for r in samples:
                if args.cuda:
                    t1 = time()
                    S_q[j] += ssf_cuda(q, r, args.block_size)
                    t2 = time()
                    S_q2[j] += _static_structure_factor(q, r)
                    t3 = time()
                    timer_gpu += t2 - t1
                    timer_host += t3 - t2
                else:
                    S_q[j] += _static_structure_factor(q, r)
        diff = abs(S_q - S_q2) / S_q
        idx = where(diff > 1e-6)
        print diff[idx], '@', q_range[idx]
        print 'GPU  execution time: %.3f s' % (timer_gpu)
        print 'Host execution time: %.3f s' % (timer_host)
        print 'Speedup: %.1f' % (timer_host / timer_gpu)

        S_q /= samples.shape[0]

        if args.label:
            label = args.label[i % len(args.label)] % attrs

        elif args.legend or not args.small:
            basename = os.path.splitext(os.path.basename(fn))[0]
            label = r'%s' % basename.replace('_', r'\_')

        if args.title:
            title = args.title % attrs

        c = args.colors[i % len(args.colors)]
        ax.plot(q_range, S_q, '-', color=c, label=label)
        ax.plot(q_range, S_q, 'o', markerfacecolor=c, markeredgecolor=c, markersize=2)
        if args.dump:
            f = open(args.dump, 'a')
            print >>f, '# %s' % label.replace(r'\_', '_')
            savetxt(f, array((q_range, S_q)).T)
            print >>f, '\n'

    # optionally plot power laws
    if args.power_law:
        p = reshape(args.power_law, (-1, 4))
        for (pow_exp, pow_coeff, pow_xmin, pow_xmax) in p:
            px = logspace(log10(pow_xmin), log10(pow_xmax), num=100)
            py = pow_coeff * pow(px, pow_exp)
            ax.plot(px, py, 'k--')

    # optionally plot with logarithmic scale(s)
    if args.axes == 'xlog':
        ax.set_xscale('log')
    if args.axes == 'ylog':
        ax.set_yscale('log')
    if args.axes == 'loglog':
        ax.set_xscale('log')
        ax.set_yscale('log')

    if args.legend or not args.small:
        l = ax.legend(loc=args.legend)
        l.legendPatch.set_alpha(0.7)

    ax.axis('tight')
    if args.xlim:
        plt.setp(ax, xlim=args.xlim)
    if args.ylim:
        plt.setp(ax, ylim=args.ylim)

    plt.xlabel(args.xlabel or r'$\lvert\textbf{q}\rvert\sigma$')
    plt.ylabel(args.ylabel or r'$S(\lvert\textbf{q}\rvert)$')

    if args.output is None:
        plt.show()
    else:
        plt.savefig(args.output, dpi=args.dpi)

def make_cuda_kernels():
    from pycuda.compiler import SourceModule
    from pycuda.reduction import ReductionKernel

    mod = SourceModule("""
    // thread ID within block
    #define TID     threadIdx.x
    // number of threads per block
    #define TDIM    blockDim.x
    // block ID within grid
    #define BID     (blockIdx.y * gridDim.x + blockIdx.x)
    // number of blocks within grid
    #define BDIM    (gridDim.y * gridDim.x)
    // thread ID within grid
    #define GTID    (BID * TDIM + TID)
    // number of threads per grid
    #define GTDIM   (BDIM * TDIM)

    // compute exp(i q·r) for a single particle
    __global__ void compute_ssf(float *sin_, float *cos_, float *q, float *r,
                                int offset, int npart, int dim)
    {
        const int i = GTID;
        if (i >= npart)
            return;

        float q_r = 0;
        for (int k=0; k < dim; k++) {
            q_r += q[k + offset * dim] * r[i + k * npart];
        }
        sin_[i] = sin(q_r);
        cos_[i] = cos(q_r);
    }
    """)
    global compute_ssf, sum_kernel

    compute_ssf = mod.get_function("compute_ssf")
#    compute_ssf.prepare("PPPPiii", block=(args.block_size, 1, 1))

    sum_kernel = ReductionKernel(float32, neutral="0",
                                 reduce_expr="a+b", map_expr="a[i]",
                                 arguments="float *a")

def ssf_cuda(q, r, block_size=64):
    from pycuda.gpuarray import GPUArray, to_gpu, zeros, take

    nq, dim = q.shape
    npart = r.shape[0]

    # CUDA execution dimensions
    block = (block_size, 1, 1)
    grid = (int(ceil(float(npart) / prod(block))), 1)

    # copy particle positions to device
    # (x0, x1, x2, ..., xN, y0, y1, y2, ..., yN, z0, z1, z2, ..., zN)
    gpu_r = to_gpu(r.T.flatten().astype(float32))

    # allocate space for results
    gpu_sin = zeros(npart, float32)
    gpu_cos = zeros(npart, float32)

    # loop over groups of wavevectors with (almost) equal magnitude
    gpu_q = to_gpu(q.flatten().astype(float32))

    # loop over wavevectors
    result = 0
    for i in range(nq):
        gpu_sin.fill(0)
        gpu_cos.fill(0)
        # compute exp(iq·r) for each particle
#       compute_ssf.prepared_call((ceil(npart/bs), 1), gpu_sin, gpu_cos, gpu_q, gpu_r, i, npart, dim)
        compute_ssf(gpu_sin, gpu_cos, gpu_q, gpu_r,
                    int32(i), int32(npart), int32(dim), block=block, grid=grid)
        # sum(sin)^2 + sum(cos)^2
        result += pow(sum_kernel(gpu_sin).get(), 2)
        result += pow(sum_kernel(gpu_cos).get(), 2)
    # normalize result with #wavevectors and #particles
    return result / (nq * npart)

def add_parser(subparsers):
    parser = subparsers.add_parser('ssf', help='static structure factor')
    parser.add_argument('input', nargs='+', metavar='INPUT', help='HDF5 trajectory file')
    parser.add_argument('--flavour', help='particle flavour')
    parser.add_argument('--sample', help='index of phase space sample(s)')
    parser.add_argument('--q-limit', type=float, help='maximum value of |q|')
    parser.add_argument('--q-error', type=float, help='relative deviation of |q|')
    parser.add_argument('--xlim', metavar='VALUE', type=float, nargs=2, help='limit x-axis to given range')
    parser.add_argument('--ylim', metavar='VALUE', type=float, nargs=2, help='limit y-axis to given range')
    parser.add_argument('--axes', choices=['xlog', 'ylog', 'loglog'], help='logarithmic scaling')
    parser.add_argument('--power-law', type=float, nargs='+', help='plot power law curve(s)')
    parser.add_argument('--cuda', action='store_true', help='use CUDA device to speed up the computation')
    parser.add_argument('--block-size', type=int, help='block size to be used for CUDA calls')
    parser.add_argument('--verbose', action='store_true')
    parser.set_defaults(sample='0', q_limit=25, q_error=0.1, block_size=64)

