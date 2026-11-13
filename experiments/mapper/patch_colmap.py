"""Patches COLMAP 4.2.1's source for the benchmark (build.sh runs it in the build's container): patch_colmap.py <colmap dir>

- Every bundle adjustment and global positioning solve logs a "BENCH_BA" / "BENCH_GP" line naming the device and
  solver it chose, so a run's log can be counted for what ran on the GPU. The mapper logs nothing of the kind itself.
- The thresholds that pick the solver, which the mapper has no option for, can be set from the environment:
  COLMAP_BENCH_MIN_GPU_IMAGES (50: below this many images a solve stays on the CPU even with ba_use_gpu),
  COLMAP_BENCH_DENSE_CPU (50), COLMAP_BENCH_SPARSE_CPU (1000), COLMAP_BENCH_DENSE_GPU (200) and
  COLMAP_BENCH_SPARSE_GPU (4000): up to this many images a solve uses dense Schur, then sparse Schur, then iterative.
- Bitmap::Write leaves out a photo's empty text metadata: OpenImageIO 2.4 aborts the whole program writing a JPEG's
  IPTC block when a field it puts there is an empty string (web photos often carry Artist "" and Copyright "" beside
  an IPTC rating), which killed image_undistorter on Drifter's photos.
Each change is made once: a tree already patched is left as it is, and one with only the first changes gets the rest.
"""
import sys
from pathlib import Path

ENV = '''#include <cstdlib>
namespace colmap {
inline int %s(const char* name, int fallback) {
  const char* value = std::getenv(name);
  return value ? std::atoi(value) : fallback;
}
}  // namespace colmap
'''


def edit(path: Path, *pairs: tuple[str, str]) -> None:
    """Replaces each old text (which must be there exactly once) with its new text."""
    s = path.read_text()
    for old, new in pairs:
        assert s.count(old) == 1, (path, old, s.count(old))
        s = s.replace(old, new)
    path.write_text(s)


def bitmap(path: Path) -> None:
    if "BENCH_EMPTY_STRINGS" in path.read_text():
        return
    edit(path, ('''  if (!output->open(utf8_path, meta_data.image_spec)) {
''', '''  // BENCH_EMPTY_STRINGS: OpenImageIO 2.4 aborts (encode_iptc_iim's assertion) writing an empty string to a
  // JPEG's IPTC block, as for the Artist "" and Copyright "" of many web photos: leave empty strings out.
  {
    auto& attribs = meta_data.image_spec.extra_attribs;
    for (auto it = attribs.end(); it != attribs.begin();) {
      --it;
      if (it->type() == OIIO::TypeString && it->nvalues() == 1 &&
          (*static_cast<const char* const*>(it->data()) == nullptr ||
           **static_cast<const char* const*>(it->data()) == '\\0')) {
        it = attribs.erase(it);
      }
    }
  }
  if (!output->open(utf8_path, meta_data.image_spec)) {
'''))


def main(colmap: Path) -> None:
    bitmap(colmap / "src/colmap/sensor/bitmap.cc")
    est = colmap / "src/colmap/estimators"
    if "BENCH_BA" in (est / "bundle_adjustment_ceres.cc").read_text():
        return
    edit(est / "bundle_adjustment_ceres.h",
         ("#include <ceres/ceres.h>\n", "#include <ceres/ceres.h>\n" + ENV % "BenchEnvBA"),
         ("int min_num_images_gpu_solver = 50;", 'int min_num_images_gpu_solver = BenchEnvBA("COLMAP_BENCH_MIN_GPU_IMAGES", 50);'),
         ("int max_num_images_direct_dense_cpu_solver = 50;",
          'int max_num_images_direct_dense_cpu_solver = BenchEnvBA("COLMAP_BENCH_DENSE_CPU", 50);'),
         ("int max_num_images_direct_sparse_cpu_solver = 1000;",
          'int max_num_images_direct_sparse_cpu_solver = BenchEnvBA("COLMAP_BENCH_SPARSE_CPU", 1000);'),
         ("int max_num_images_direct_dense_gpu_solver = 200;",
          'int max_num_images_direct_dense_gpu_solver = BenchEnvBA("COLMAP_BENCH_DENSE_GPU", 200);'),
         ("int max_num_images_direct_sparse_gpu_solver = 4000;",
          'int max_num_images_direct_sparse_gpu_solver = BenchEnvBA("COLMAP_BENCH_SPARSE_GPU", 4000);'))
    edit(est / "bundle_adjustment_ceres.cc",
         ("  std::string solver_error;\n  THROW_CHECK(custom_solver_options.IsValid(&solver_error)) << solver_error;\n",
          '''  {
    bool bench_cuda = false;
#ifdef COLMAP_CUDA_ENABLED
    bench_cuda = cuda_solver_enabled;
#endif
    LOG(INFO) << "BENCH_BA images=" << num_images
              << " residuals=" << problem.NumResiduals()
              << " device=" << (bench_cuda ? "cuda" : "cpu") << " solver="
              << ceres::LinearSolverTypeToString(
                     custom_solver_options.linear_solver_type)
              << " dense="
              << ceres::DenseLinearAlgebraLibraryTypeToString(
                     custom_solver_options.dense_linear_algebra_library_type)
              << " sparse="
              << ceres::SparseLinearAlgebraLibraryTypeToString(
                     custom_solver_options.sparse_linear_algebra_library_type)
              << " threads=" << custom_solver_options.num_threads;
  }
  std::string solver_error;
  THROW_CHECK(custom_solver_options.IsValid(&solver_error)) << solver_error;
'''))
    edit(est / "bundle_adjustment_caspar.cc",
         ("    auto solver = CreateSolver(params, BuildSizing(), device_id);\n",
          '''    LOG(INFO) << "BENCH_BA images=" << TotalPoses() << " points=" << num_points_
              << " device=cuda solver=CASPAR gpu=" << device_id
#ifdef CASPAR_USE_DOUBLE
              << " precision=float64";
#else
              << " precision=float32";
#endif
    auto solver = CreateSolver(params, BuildSizing(), device_id);
'''))
    edit(est / "global_positioning.h",
         ("#include <ceres/ceres.h>\n", "#include <ceres/ceres.h>\n" + ENV % "BenchEnvGP"),
         ("int min_num_images_gpu_solver = 50;", 'int min_num_images_gpu_solver = BenchEnvGP("COLMAP_BENCH_MIN_GPU_IMAGES", 50);'))
    edit(est / "global_positioning.cc",
         ("  options_.solver_options.linear_solver_type = ceres::SPARSE_SCHUR;\n}\n",
          '''  options_.solver_options.linear_solver_type = ceres::SPARSE_SCHUR;
  {
    bool bench_cuda = false;
#ifdef COLMAP_CUDA_ENABLED
    bench_cuda = cuda_solver_enabled;
#endif
    LOG(INFO) << "BENCH_GP images=" << reconstruction.NumImages()
              << " device=" << (bench_cuda ? "cuda" : "cpu")
              << " solver=SPARSE_SCHUR sparse="
              << ceres::SparseLinearAlgebraLibraryTypeToString(
                     options_.solver_options.sparse_linear_algebra_library_type);
  }
}
'''))
    print("patched", colmap)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
