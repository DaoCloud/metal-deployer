/*
 * imex_fabric_ipc_test - minimal cross-node NVLink memory import/export test.
 *
 * Verifies that the NVIDIA IMEX service actually works: rank 0 allocates GPU
 * memory with a FABRIC shareable handle, every other rank (ideally on another
 * compute tray) imports it, writes a rank-specific pattern into its own slice
 * directly over NVLink, and rank 0 verifies the result. Optionally times a
 * device-to-device copy into the imported buffer to report write bandwidth.
 *
 * Build:  mpicc -O2 -o imex_fabric_ipc_test imex_fabric_ipc_test.c \
 *               -I$CUDA_HOME/include -L$CUDA_HOME/lib64 -lcuda
 * Run:    mpirun -np 2 -H nodeA:1,nodeB:1 ./imex_fabric_ipc_test [--size MB] [--iters N]
 *
 * Exit 0 = PASS, 1 = FAIL, 2 = unsupported (no fabric handle support / no IMEX).
 */
#include <cuda.h>
#include <mpi.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CU(call)                                                                      \
    do {                                                                              \
        CUresult _r = (call);                                                         \
        if (_r != CUDA_SUCCESS) {                                                     \
            const char *_n = NULL, *_s = NULL;                                        \
            cuGetErrorName(_r, &_n);                                                  \
            cuGetErrorString(_r, &_s);                                                \
            fprintf(stderr, "[rank %d %s] %s failed: %s (%s)\n", g_rank, g_host, #call, \
                    _n ? _n : "?", _s ? _s : "?");                                    \
            MPI_Abort(MPI_COMM_WORLD, 1);                                             \
        }                                                                             \
    } while (0)

static int g_rank = -1;
static char g_host[64] = "?";

static int local_rank(void)
{
    const char *keys[] = {"OMPI_COMM_WORLD_LOCAL_RANK", "MPI_LOCALRANKID", "SLURM_LOCALID", "PMI_RANK", NULL};
    for (int i = 0; keys[i]; i++) {
        const char *v = getenv(keys[i]);
        if (v) return atoi(v);
    }
    MPI_Comm node;
    int lr = 0;
    MPI_Comm_split_type(MPI_COMM_WORLD, MPI_COMM_TYPE_SHARED, 0, MPI_INFO_NULL, &node);
    MPI_Comm_rank(node, &lr);
    MPI_Comm_free(&node);
    return lr;
}

int main(int argc, char **argv)
{
    size_t size_mb = 64;
    int iters = 20;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--size") && i + 1 < argc) size_mb = (size_t)atol(argv[++i]);
        else if (!strcmp(argv[i], "--iters") && i + 1 < argc) iters = atoi(argv[++i]);
    }

    MPI_Init(&argc, &argv);
    int nranks;
    MPI_Comm_rank(MPI_COMM_WORLD, &g_rank);
    MPI_Comm_size(MPI_COMM_WORLD, &nranks);
    gethostname(g_host, sizeof(g_host) - 1);

    if (nranks < 2) {
        if (g_rank == 0) fprintf(stderr, "need at least 2 ranks (2 nodes for a cross-node test)\n");
        MPI_Finalize();
        return 1;
    }

    CU(cuInit(0));
    int ndev = 0;
    CU(cuDeviceGetCount(&ndev));
    int dev_idx = local_rank() % (ndev > 0 ? ndev : 1);
    CUdevice dev;
    CU(cuDeviceGet(&dev, dev_idx));
    CUcontext ctx;
    CU(cuDevicePrimaryCtxRetain(&ctx, dev));
    CU(cuCtxSetCurrent(ctx));

    int fabric_ok = 0;
    CU(cuDeviceGetAttribute(&fabric_ok, CU_DEVICE_ATTRIBUTE_HANDLE_TYPE_FABRIC_SUPPORTED, dev));
    int all_ok = 0;
    MPI_Allreduce(&fabric_ok, &all_ok, 1, MPI_INT, MPI_MIN, MPI_COMM_WORLD);
    if (!all_ok) {
        if (!fabric_ok)
            fprintf(stderr, "[rank %d %s] GPU %d: FABRIC handle type not supported "
                            "(IMEX daemon not running, channel0 missing, or GPU not in an NVLink domain)\n",
                    g_rank, g_host, dev_idx);
        MPI_Finalize();
        return 2;
    }

    /* Hostname / GPU UUID table so the log shows which trays took part. */
    char uuid_str[64] = {0};
    CUuuid uuid;
    CU(cuDeviceGetUuid(&uuid, dev));
    for (int i = 0; i < 16; i++) snprintf(uuid_str + i * 2, 3, "%02x", (unsigned char)uuid.bytes[i]);
    char line[192];
    snprintf(line, sizeof(line), "%-20s gpu%d GPU-%.8s", g_host, dev_idx, uuid_str);
    char *all_lines = g_rank == 0 ? malloc((size_t)nranks * sizeof(line)) : NULL;
    MPI_Gather(line, sizeof(line), MPI_CHAR, all_lines, sizeof(line), MPI_CHAR, 0, MPI_COMM_WORLD);
    if (g_rank == 0) {
        printf("imex_fabric_ipc_test: %d ranks, buffer %zu MiB, %d timing iterations\n", nranks, size_mb, iters);
        for (int r = 0; r < nranks; r++) printf("  rank %-3d %s%s\n", r, all_lines + (size_t)r * sizeof(line), r == 0 ? "  (exporter)" : "");
        free(all_lines);
    }

    /* Allocation with FABRIC handle type, rounded to granularity. */
    CUmemAllocationProp prop;
    memset(&prop, 0, sizeof(prop));
    prop.type = CU_MEM_ALLOCATION_TYPE_PINNED;
    prop.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    prop.location.id = dev;
    prop.requestedHandleTypes = CU_MEM_HANDLE_TYPE_FABRIC;
    size_t gran = 0;
    CU(cuMemGetAllocationGranularity(&gran, &prop, CU_MEM_ALLOC_GRANULARITY_RECOMMENDED));
    size_t size = size_mb << 20;
    size = (size + gran - 1) / gran * gran;
    size_t slice = size / (size_t)nranks / sizeof(uint32_t) * sizeof(uint32_t);

    CUmemGenericAllocationHandle h = 0;
    CUmemFabricHandle fh;
    memset(&fh, 0, sizeof(fh));
    if (g_rank == 0) {
        CU(cuMemCreate(&h, size, &prop, 0));
        CU(cuMemExportToShareableHandle(&fh, h, CU_MEM_HANDLE_TYPE_FABRIC, 0));
    }
    MPI_Bcast(&fh, (int)sizeof(fh), MPI_BYTE, 0, MPI_COMM_WORLD);
    if (g_rank != 0) {
        CUresult r = cuMemImportFromShareableHandle(&h, &fh, CU_MEM_HANDLE_TYPE_FABRIC);
        if (r != CUDA_SUCCESS) {
            const char *n = NULL;
            cuGetErrorName(r, &n);
            fprintf(stderr, "[rank %d %s] cuMemImportFromShareableHandle failed: %s "
                            "(check nvidia-imex-ctl -N on both nodes and /var/log/nvidia-imex.log)\n",
                    g_rank, g_host, n ? n : "?");
            MPI_Abort(MPI_COMM_WORLD, 1);
        }
    }

    CUdeviceptr base = 0;
    CU(cuMemAddressReserve(&base, size, 0, 0, 0));
    CU(cuMemMap(base, size, 0, h, 0));
    CUmemAccessDesc acc;
    memset(&acc, 0, sizeof(acc));
    acc.location.type = CU_MEM_LOCATION_TYPE_DEVICE;
    acc.location.id = dev;
    acc.flags = CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
    CU(cuMemSetAccess(base, size, &acc, 1));

    if (g_rank == 0) CU(cuMemsetD32(base, 0, size / sizeof(uint32_t)));
    CU(cuCtxSynchronize());
    MPI_Barrier(MPI_COMM_WORLD);

    /* Every rank writes its pattern into its slice; ranks != 0 do so over NVLink. */
    uint32_t pattern = 0xA5000000u | (uint32_t)g_rank;
    CU(cuMemsetD32(base + (CUdeviceptr)((size_t)g_rank * slice), pattern, slice / sizeof(uint32_t)));
    CU(cuCtxSynchronize());
    MPI_Barrier(MPI_COMM_WORLD);

    int fail = 0;
    if (g_rank == 0) {
        uint32_t *host = malloc(size);
        CU(cuMemcpyDtoH(host, base, size));
        for (int r = 0; r < nranks; r++) {
            size_t words = slice / sizeof(uint32_t);
            size_t off = (size_t)r * words;
            size_t bad = 0;
            for (size_t i = 0; i < words; i++)
                if (host[off + i] != (0xA5000000u | (uint32_t)r)) bad++;
            printf("  slice from rank %-3d: %s%s\n", r, bad ? "FAIL" : "PASS",
                   bad ? " (mismatch words)" : "");
            if (bad) fail = 1;
        }
        free(host);
    }
    MPI_Bcast(&fail, 1, MPI_INT, 0, MPI_COMM_WORLD);

    /* Bandwidth: each importer copies a local buffer into the whole remote allocation. */
    if (!fail && iters > 0 && g_rank != 0) {
        CUdeviceptr src = 0;
        CU(cuMemAlloc(&src, size));
        CUevent e0, e1;
        CU(cuEventCreate(&e0, CU_EVENT_DEFAULT));
        CU(cuEventCreate(&e1, CU_EVENT_DEFAULT));
        CU(cuMemcpyDtoDAsync(base, src, size, 0)); /* warm-up */
        CU(cuEventRecord(e0, 0));
        for (int i = 0; i < iters; i++) CU(cuMemcpyDtoDAsync(base, src, size, 0));
        CU(cuEventRecord(e1, 0));
        CU(cuEventSynchronize(e1));
        float ms = 0;
        CU(cuEventElapsedTime(&ms, e0, e1));
        double gbps = (double)size * iters / (ms / 1e3) / 1e9;
        printf("  rank %-3d %-20s -> rank 0 write: %8.1f GB/s (%zu MiB x %d)\n", g_rank, g_host, gbps, size >> 20, iters);
        CU(cuEventDestroy(e0));
        CU(cuEventDestroy(e1));
        CU(cuMemFree(src));
    }
    MPI_Barrier(MPI_COMM_WORLD);

    CU(cuMemUnmap(base, size));
    CU(cuMemAddressFree(base, size));
    CU(cuMemRelease(h));
    CU(cuDevicePrimaryCtxRelease(dev));

    if (g_rank == 0) printf("imex_fabric_ipc_test: %s\n", fail ? "FAIL" : "PASS");
    MPI_Finalize();
    return fail;
}
