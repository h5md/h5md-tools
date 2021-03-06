/* mdplot - Molecular Dynamics simulation plotter
 *
 * Copyright © 2008-2010  Peter Colberg, Felix Höfling
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License
 * as published by the Free Software Foundation; either version 2
 * of the License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA 02110-1301, USA.
 */

#define MAX_BLOCK_SIZE 512

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

// store q vectors in texture
texture<float, 1> tex_q;

// global constants
__constant__ uint npart;     // number of particles
__constant__ uint nq;        // number of wavevectors
__constant__ uint dim;       // space dimension

// copy enable_if_c and disable_if_c from Boost.Utility
// to avoid dependency on Boost headers
template <bool B, class T = void>
struct enable_if_c {
    typedef T type;
};

template <class T>
struct enable_if_c<false, T> {};

template <bool B, class T = void>
struct disable_if_c {
    typedef T type;
};

template <class T>
struct disable_if_c<true, T> {};

// recursive reduction function,
// terminate for threads=0
template <unsigned threads, typename T>
__device__ typename disable_if_c<threads>::type
sum_reduce(T*, T*) {}

// reduce two array simultaneously by summation,
// size of a,b must be at least 2 * threads
template <unsigned threads, typename T>
__device__ typename enable_if_c<threads>::type
sum_reduce(T* a, T* b)
{
    if (TID < threads) {
        a[TID] += a[TID + threads];
        b[TID] += b[TID + threads];
    }
    if (threads >= warpSize) {
        __syncthreads();
    }

    // recursion ends by calling sum_reduce<0>
    sum_reduce<threads / 2>(a, b);
}

/* FIXME
typedef void (*sum_reduce_type)(float*, float*);
__device__ sum_reduce_type sum_reduce_select[] = {
    &sum_reduce<0>, &sum_reduce<1>, &sum_reduce<2>, &sum_reduce<4>,
    &sum_reduce<8>, &sum_reduce<16>, &sum_reduce<32>, &sum_reduce<64>,
    &sum_reduce<128>, &sum_reduce<256>
};
*/

extern "C" {

// compute exp(i q·r) for a single particle and for a set of wavevectors,
// return block sum of results
__global__ void compute_ssf(float* sin_block, float* cos_block, float const* r)
{
    __shared__ float sin_[MAX_BLOCK_SIZE];
    __shared__ float cos_[MAX_BLOCK_SIZE];

    // outer loop over wavevectors
    for (uint i=0; i < nq; i++) {
        sin_[TID] = 0;
        cos_[TID] = 0;
        for (uint j = GTID; j < npart; j += GTDIM) {
            // compute scalar product q·r
            float q_r = 0;
            for (uint k=0; k < dim; k++) {
                // particle positions are stored in 'Fortran order'
                q_r += tex1Dfetch(tex_q, i * dim + k) * r[j + k * npart];
            }
            sin_[TID] += sin(q_r);
            cos_[TID] += cos(q_r);
        }
        __syncthreads();

        // accumulate results within block
        if (TDIM == 512) sum_reduce<256>(sin_, cos_);
        else if (TDIM == 256) sum_reduce<128>(sin_, cos_);
        else if (TDIM == 128) sum_reduce<64>(sin_, cos_);
        else if (TDIM == 64) sum_reduce<32>(sin_, cos_);
        else if (TDIM == 32) sum_reduce<16>(sin_, cos_);
        else if (TDIM == 16) sum_reduce<8>(sin_, cos_);
        else if (TDIM == 8) sum_reduce<4>(sin_, cos_);

        if (TID == 0) {
            sin_block[i * BDIM + BID] = sin_[0];
            cos_block[i * BDIM + BID] = cos_[0];
        }
        __syncthreads();
    }
}

// compute the remaining sum, square and add sin and cos parts
// for each wavevector separately, finally sum everything
// ssf = sum(sin(q·r))^2 + sum(cos(q·r))^2
// 
// the final result is stored in 'ssf'
// 'bdim' is the number of blocks (grid size) in the preceding call to compute_ssf()
__global__ void finalise_ssf(float* sin_block, float* cos_block, float* ssf, uint bdim)
{
    __shared__ float s_sum[MAX_BLOCK_SIZE];
    __shared__ float c_sum[MAX_BLOCK_SIZE];

    float result = 0;
    // outer loop over wavevectors, distribute over block grid
    for (uint i = BID; i < nq; i += BDIM) {
        s_sum[TID] = 0;
        c_sum[TID] = 0;
        for (uint j = TID; j < bdim; j += TDIM) {
            s_sum[TID] += sin_block[i * bdim + TID];
            c_sum[TID] += cos_block[i * bdim + TID];
        }
        __syncthreads();

        // accumulate results within block
        if (TDIM == 512) sum_reduce<256>(s_sum, c_sum);
        else if (TDIM == 256) sum_reduce<128>(s_sum, c_sum);
        else if (TDIM == 128) sum_reduce<64>(s_sum, c_sum);
        else if (TDIM == 64) sum_reduce<32>(s_sum, c_sum);
        else if (TDIM == 32) sum_reduce<16>(s_sum, c_sum);
        else if (TDIM == 16) sum_reduce<8>(s_sum, c_sum);
        else if (TDIM == 8) sum_reduce<4>(s_sum, c_sum);

        // compute square 
        if (TID == 0) {
            result += s_sum[0] * s_sum[0] + c_sum[0] * c_sum[0];
        }
        __syncthreads();
    }
    // store result in global memory
    if (TID == 0) {
        ssf[BID] = result;
    }
}

}  // extern "C"

