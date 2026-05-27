#!/usr/bin/env python3
"""GitHub Actions 多线程文件操作性能实验.

在 GitHub 官方虚拟机环境下，对 1 GiB / 10,000 个文本文件执行
生成、压缩、复制、解压四项操作的多线程性能基准测试.
"""

import json
import os
import platform
import random
import shutil
import string
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# =============================================================================
# 实验参数（常量）
# =============================================================================

RANDOM_SEED = 42
TOTAL_FILES = 10_000
TOTAL_SIZE_BYTES = 1_073_741_824  # 严格 1 GiB
MIN_DEPTH = 1
MAX_DEPTH = 9
MIN_FILE_SIZE = 1_024  # 1 KiB
MAX_FILE_SIZE = 1_048_576  # 1 MiB
THREAD_COUNTS = [1, 2, 4, 8, 16]
FILE_EXTENSIONS = (".txt", ".py", ".json", ".md", ".csv", ".log")

TEST_DIR = Path("test_data")
RESULTS_DIR = Path("results")
ZIP_PATH = TEST_DIR / "archive.zip"

random.seed(RANDOM_SEED)


# =============================================================================
# 数据结构
# =============================================================================

@dataclass
class FileSpec:
    """单个测试文件的规格定义."""

    file_id: int
    relative_path: str
    size_bytes: int


@dataclass
class BenchmarkResult:
    """单次基准测试结果."""

    thread_count: int
    elapsed_sec: float
    total_mb: float
    speed_mbps: float
    tool: str = "python"
    fallback: bool = False
    note: str = ""


@dataclass
class SystemInfo:
    """系统环境与工具链信息."""

    system: str
    release: str
    version: str
    machine: str
    processor: str
    python_version: str
    has_7zip: bool
    seven_zip_version: Optional[str] = None


@dataclass
class ExperimentResults:
    """完整实验结果."""

    system_info: SystemInfo
    parameters: dict
    file_generation: List[BenchmarkResult] = field(default_factory=list)
    zip_compression: List[BenchmarkResult] = field(default_factory=list)
    file_copy: List[BenchmarkResult] = field(default_factory=list)
    zip_extract: List[BenchmarkResult] = field(default_factory=list)


# =============================================================================
# 工具函数
# =============================================================================

def get_system_info() -> SystemInfo:
    """采集当前系统信息与 7-Zip 可用性."""
    has_7z = shutil.which("7z") is not None
    seven_zip_ver: Optional[str] = None

    if has_7z:
        try:
            proc = subprocess.run(
                ["7z"], capture_output=True, text=True, check=False
            )
            if proc.stdout:
                seven_zip_ver = proc.stdout.splitlines()[0]
        except Exception:
            has_7z = False

    return SystemInfo(
        system=platform.system(),
        release=platform.release(),
        version=platform.version(),
        machine=platform.machine(),
        processor=platform.processor(),
        python_version=platform.python_version(),
        has_7zip=has_7z,
        seven_zip_version=seven_zip_ver,
    )


def random_content(size: int) -> str:
    """生成指定长度的随机文本内容."""
    chars = string.ascii_letters + string.digits + string.punctuation + " \n"
    return "".join(random.choices(chars, k=size))


def generate_file_specs() -> List[FileSpec]:
    """生成 10,000 个文件规格，严格保证总大小为 1 GiB."""
    specs: List[FileSpec] = []
    remaining_bytes = TOTAL_SIZE_BYTES
    remaining_files = TOTAL_FILES

    for i in range(TOTAL_FILES):
        if i == TOTAL_FILES - 1:
            size = remaining_bytes
        else:
            max_possible = min(
                MAX_FILE_SIZE,
                remaining_bytes - (remaining_files - 1) * MIN_FILE_SIZE,
            )
            size = random.randint(MIN_FILE_SIZE, max_possible)

        depth = random.randint(MIN_DEPTH, MAX_DEPTH)
        dirs = [f"dir_{random.randint(0, 99):02d}" for _ in range(depth)]
        rel_path = os.path.join(*dirs) if dirs else ""
        ext = random.choice(FILE_EXTENSIONS)
        filename = f"file_{i:05d}{ext}"

        specs.append(
            FileSpec(
                file_id=i,
                relative_path=os.path.join(rel_path, filename),
                size_bytes=size,
            )
        )

        remaining_bytes -= size
        remaining_files -= 1

    return specs


# =============================================================================
# 文件操作原子函数
# =============================================================================

def write_single_file(base_dir: Path, spec: FileSpec) -> int:
    """将单个文件写入磁盘."""
    full_path = base_dir / spec.relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    content = random_content(spec.size_bytes)
    full_path.write_text(content, encoding="utf-8")
    return spec.size_bytes


def copy_single_file(src: Path, dest: Path) -> int:
    """复制单个文件并返回字节数."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest.stat().st_size


# =============================================================================
# 基准测试执行器
# =============================================================================

def run_with_threads(
    items: List[Tuple],
    worker: Callable,
    thread_count: int,
) -> int:
    """通用多线程执行封装，返回总处理字节数."""
    total = 0

    if thread_count == 1:
        for args in items:
            total += worker(*args)
        return total

    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = [executor.submit(worker, *args) for args in items]
        for future in as_completed(futures):
            total += future.result()

    return total


def timed_benchmark(
    thread_count: int,
    prepare: Callable[[], None],
    work: Callable[[], int],
    tool: str = "python",
    note: str = "",
    fallback: bool = False,
) -> BenchmarkResult:
    """统一计时包装器."""
    prepare()
    start = time.perf_counter()
    total_bytes = work()
    elapsed = time.perf_counter() - start

    total_mb = total_bytes / (1024 * 1024)
    speed = total_mb / elapsed if elapsed > 0 else 0.0

    return BenchmarkResult(
        thread_count=thread_count,
        elapsed_sec=round(elapsed, 3),
        total_mb=round(total_mb, 2),
        speed_mbps=round(speed, 2),
        tool=tool,
        fallback=fallback,
        note=note,
    )


# =============================================================================
# 四大测试模块
# =============================================================================

def benchmark_file_generation(
    specs: List[FileSpec], thread_count: int
) -> BenchmarkResult:
    """测试1：多线程文件生成."""

    def prepare() -> None:
        if TEST_DIR.exists():
            shutil.rmtree(TEST_DIR)
        TEST_DIR.mkdir(parents=True, exist_ok=True)

    def work() -> int:
        items = [(TEST_DIR, spec) for spec in specs]
        return run_with_threads(items, write_single_file, thread_count)

    return timed_benchmark(thread_count, prepare, work)


def benchmark_zip_compression(
    thread_count: int, use_7zip: bool
) -> BenchmarkResult:
    """测试2：ZIP 压缩（优先 7-Zip，否则降级 Python zipfile）."""
    if use_7zip:
        return _benchmark_zip_with_7z(thread_count)
    return _benchmark_zip_with_python(thread_count)


def _benchmark_zip_with_python(thread_count: int) -> BenchmarkResult:
    """使用 Python 标准库 zipfile 执行压缩."""

    def prepare() -> None:
        if ZIP_PATH.exists():
            ZIP_PATH.unlink()

    def work() -> int:
        file_paths = [p for p in TEST_DIR.rglob("*") if p.is_file()]
        with zipfile.ZipFile(
            ZIP_PATH, "w", zipfile.ZIP_DEFLATED
        ) as zf:
            for fp in file_paths:
                zf.write(fp, arcname=fp.relative_to(TEST_DIR))
        return TOTAL_SIZE_BYTES

    note = "zipfile 不支持并发写入，thread>1 时实际为单线程"
    return timed_benchmark(
        thread_count, prepare, work, tool="python_zipfile", note=note
    )


def _benchmark_zip_with_7z(thread_count: int) -> BenchmarkResult:
    """使用 7-Zip 多线程压缩."""

    def prepare() -> None:
        if ZIP_PATH.exists():
            ZIP_PATH.unlink()

    def work() -> int:
        cmd = [
            "7z", "a",
            "-tzip",
            f"-mmt{thread_count}",
            "-mx=1",  # 最快压缩级别
            str(ZIP_PATH),
            str(TEST_DIR / "*"),
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return TOTAL_SIZE_BYTES

    result = timed_benchmark(
        thread_count, prepare, work, tool="7zip"
    )

    if not ZIP_PATH.exists():
        print("  [降级] 7z 失败，回退到 Python zipfile")
        return _benchmark_zip_with_python(thread_count)

    return result


def benchmark_file_copy(thread_count: int) -> BenchmarkResult:
    """测试3：多线程文件复制."""
    dest_dir = Path("test_data_copy")

    def prepare() -> None:
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

    def work() -> int:
        src_files = [p for p in TEST_DIR.rglob("*") if p.is_file()]
        items = [
            (src, dest_dir / src.relative_to(TEST_DIR))
            for src in src_files
        ]
        total = run_with_threads(items, copy_single_file, thread_count)
        shutil.rmtree(dest_dir, ignore_errors=True)
        return total

    return timed_benchmark(thread_count, prepare, work)


def benchmark_zip_extract(
    thread_count: int, use_7zip: bool
) -> BenchmarkResult:
    """测试4：ZIP 解压（优先 7-Zip，否则降级 Python zipfile）."""
    extract_dir = Path("test_data_extracted")

    def prepare() -> None:
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True, exist_ok=True)

    def work_python() -> int:
        with zipfile.ZipFile(ZIP_PATH, "r") as zf:
            zf.extractall(extract_dir)
        total = sum(
            f.stat().st_size for f in extract_dir.rglob("*") if f.is_file()
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        return total

    def work_7z() -> int:
        cmd = [
            "7z", "x",
            str(ZIP_PATH),
            f"-o{extract_dir}",
            f"-mmt{thread_count}",
            "-y",
        ]
        subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        total = sum(
            f.stat().st_size for f in extract_dir.rglob("*") if f.is_file()
        )
        shutil.rmtree(extract_dir, ignore_errors=True)
        return total

    if use_7zip:
        result = timed_benchmark(
            thread_count, prepare, work_7z, tool="7zip"
        )
        if not any(extract_dir.iterdir()):
            print("  [降级] 7z 解压失败，回退到 Python zipfile")
            return timed_benchmark(
                thread_count,
                prepare,
                work_python,
                tool="python_zipfile",
                fallback=True,
            )
        return result

    note = "zipfile 解压为单线程操作"
    return timed_benchmark(
        thread_count, prepare, work_python, tool="python_zipfile", note=note
    )


# =============================================================================
# 主控流程
# =============================================================================

def ensure_source_files(specs: List[FileSpec]) -> None:
    """确保测试源文件已生成."""
    if not TEST_DIR.exists() or not any(TEST_DIR.iterdir()):
        print("  [准备] 源文件缺失，执行 4 线程预生成...")
        benchmark_file_generation(specs, thread_count=4)


def ensure_zip_archive(use_7zip: bool) -> None:
    """确保 ZIP 压缩包已存在."""
    if not ZIP_PATH.exists():
        print("  [准备] 压缩包缺失，执行 4 线程预压缩...")
        benchmark_zip_compression(thread_count=4, use_7zip=use_7zip)


def run_single_test(
    name: str,
    test_func: Callable[[int], BenchmarkResult],
    thread_counts: List[int],
) -> List[BenchmarkResult]:
    """执行单类测试的所有线程梯度."""
    print(f"\n[{name}]")
    results: List[BenchmarkResult] = []

    for tc in thread_counts:
        print(f"  线程={tc:2d} ... ", end="", flush=True)
        result = test_func(tc)
        flag = " [降级]" if result.fallback else ""
        print(
            f"耗时={result.elapsed_sec:7.3f}s "
            f"速度={result.speed_mbps:7.2f} MB/s "
            f"工具={result.tool}{flag}"
        )
        results.append(result)

    return results


def cleanup_all() -> None:
    """清理所有临时目录."""
    for path in (TEST_DIR, Path("test_data_copy"), Path("test_data_extracted")):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def run_experiment() -> ExperimentResults:
    """执行完整实验并返回结构化结果."""
    print("=" * 60)
    print("GitHub Actions 多线程文件操作性能实验")
    print("=" * 60)

    sys_info = get_system_info()
    print(f"\n[系统信息]\n{json.dumps(asdict(sys_info), indent=2)}\n")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    specs = generate_file_specs()

    results = ExperimentResults(
        system_info=sys_info,
        parameters={
            "total_files": TOTAL_FILES,
            "total_size_bytes": TOTAL_SIZE_BYTES,
            "file_extensions": list(FILE_EXTENSIONS),
            "min_depth": MIN_DEPTH,
            "max_depth": MAX_DEPTH,
            "min_file_size": MIN_FILE_SIZE,
            "max_file_size": MAX_FILE_SIZE,
            "thread_counts": THREAD_COUNTS,
        },
    )

    # 测试1：文件生成
    results.file_generation = run_single_test(
        "文件生成",
        lambda tc: benchmark_file_generation(specs, tc),
        THREAD_COUNTS,
    )

    # 预准备：确保后续测试有源文件
    ensure_source_files(specs)

    # 测试2：ZIP 压缩
    results.zip_compression = run_single_test(
        "ZIP 压缩",
        lambda tc: benchmark_zip_compression(tc, sys_info.has_7zip),
        THREAD_COUNTS,
    )

    # 测试3：文件复制
    ensure_source_files(specs)
    results.file_copy = run_single_test(
        "文件复制", lambda tc: benchmark_file_copy(tc), THREAD_COUNTS
    )

    # 测试4：ZIP 解压
    ensure_zip_archive(sys_info.has_7zip)
    results.zip_extract = run_single_test(
        "ZIP 解压",
        lambda tc: benchmark_zip_extract(tc, sys_info.has_7zip),
        THREAD_COUNTS,
    )

    # 保存结果
    output = RESULTS_DIR / f"benchmark_{sys_info.system}.json"
    with open(output, "w", encoding="utf-8") as f:
        json.dump(asdict(results), f, indent=2)

    print(f"\n[完成] 结果已保存: {output}")

    cleanup_all()
    return results


if __name__ == "__main__":
    run_experiment()