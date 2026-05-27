#!/usr/bin/env python3
"""合并多系统实验结果，生成 Markdown 分析报告."""

import glob
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


# =============================================================================
# 数据加载
# =============================================================================

@dataclass
class LoadedResult:
    """单个系统的原始实验数据."""

    system_name: str
    raw_data: dict


def load_all_results(artifacts_dir: str = "artifacts") -> List[LoadedResult]:
    """从 artifacts 目录加载所有系统的实验结果."""
    results: List[LoadedResult] = []
    pattern = f"{artifacts_dir}/benchmark-*/benchmark_*.json"

    for filepath in glob.glob(pattern):
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        system = data["system_info"]["system"]
        display_name = _system_display_name(system)
        results.append(LoadedResult(system_name=display_name, raw_data=data))

    # 固定排序：Ubuntu -> Windows -> macOS
    order = {"Ubuntu-latest": 0, "Windows-latest": 1, "macOS-latest": 2}
    results.sort(key=lambda r: order.get(r.system_name, 99))
    return results


def _system_display_name(system_key: str) -> str:
    """将 platform.system() 输出映射为显示名称."""
    mapping = {
        "Linux": "Ubuntu-latest",
        "Windows": "Windows-latest",
        "Darwin": "macOS-latest",
    }
    return mapping.get(system_key, system_key)


# =============================================================================
# Markdown 生成器
# =============================================================================

class MarkdownReport:
    """Markdown 报告构建器."""

    def __init__(self) -> None:
        self._lines: List[str] = []

    def add(self, text: str = "") -> None:
        """添加一行文本."""
        self._lines.append(text)

    def heading(self, level: int, text: str) -> None:
        """添加标题."""
        self._lines.append(f"{'#' * level} {text}\n")

    def table(self, headers: List[str], rows: List[List[str]]) -> None:
        """添加 Markdown 表格."""
        self._lines.append("| " + " | ".join(headers) + " |")
        self._lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for row in rows:
            self._lines.append("| " + " | ".join(str(c) for c in row) + " |")
        self._lines.append("")

    def code_block(self, language: str, content: str) -> None:
        """添加代码块."""
        self._lines.append(f"```{language}")
        self._lines.append(content)
        self._lines.append("```\n")

    def build(self) -> str:
        """返回完整 Markdown 文本."""
        return "\n".join(self._lines)


# =============================================================================
# 表格渲染辅助
# =============================================================================

def render_thread_table(
    test_data: List[dict],
) -> tuple[List[str], List[List[str]]]:
    """渲染线程梯度表格的表头和数据行."""
    headers = ["线程数", "耗时 (s)", "速度 (MB/s)", "工具", "备注"]
    rows: List[List[str]] = []

    speeds = [d["speed_mbps"] for d in test_data]
    best_speed = max(speeds) if speeds else 0
    worst_speed = min(speeds) if speeds else 0

    for d in test_data:
        note_parts: List[str] = []
        if d.get("fallback"):
            note_parts.append("⚠️ 降级单线程")
        if d.get("note"):
            note_parts.append(d["note"])

        if d["speed_mbps"] == best_speed:
            note_parts.append("🏆 最优")
        if d["speed_mbps"] == worst_speed:
            note_parts.append("❌ 最差")

        rows.append([
            str(d["thread_count"]),
            str(d["elapsed_sec"]),
            str(d["speed_mbps"]),
            d.get("tool", "python"),
            "; ".join(note_parts),
        ])

    return headers, rows


def render_cross_system_table(
    results: List[LoadedResult],
    operation_key: str,
) -> str:
    """渲染 系统×线程 横向对比表.

    表头：三种操作系统
    第0列：线程数
    内容：时间/速度
    """
    report = MarkdownReport()

    headers = ["线程数", "Ubuntu-latest", "Windows-latest", "macOS-latest"]
    rows: List[List[str]] = []

    for tc in [1, 2, 4, 8, 16]:
        row: List[str] = [str(tc)]
        for res in results:
            test_data = res.raw_data["tests"][operation_key]
            point = next(
                (d for d in test_data if d["thread_count"] == tc), None
            )
            if point:
                cell = (f"{point['elapsed_sec']}s / "
                        f"{point['speed_mbps']}MB/s")
                if point.get("fallback") or point.get("note"):
                    cell += " ⚠️"
                row.append(cell)
            else:
                row.append("N/A")
        rows.append(row)

    report.table(headers, rows)
    return report.build()


# =============================================================================
# 报告内容生成
# =============================================================================

def generate_summary_table(results: List[LoadedResult]) -> str:
    """生成最优性能汇总表."""
    report = MarkdownReport()
    report.heading(3, "1. 汇总总表")
    report.add("> 展示各操作在不同系统下的最优性能档位。")

    headers = [
        "运行环境",
        "文件生成(最优)",
        "ZIP压缩(最优)",
        "文件复制(最优)",
        "ZIP解压(最优)",
    ]
    rows: List[List[str]] = []

    for res in results:
        data = res.raw_data["tests"]

        def best_str(test_list: List[dict]) -> str:
            best = max(test_list, key=lambda x: x["speed_mbps"])
            return (f"{best['elapsed_sec']}s / {best['speed_mbps']} MB/s "
                    f"(T{best['thread_count']})")

        rows.append([
            res.system_name,
            best_str(data["file_generation"]),
            best_str(data["zip_compression"]),
            best_str(data["file_copy"]),
            best_str(data["zip_extract"]),
        ])

    report.table(headers, rows)
    return report.build()


def generate_detail_tables(results: List[LoadedResult]) -> str:
    """生成四个分模块详细表格（纵向+横向）."""
    report = MarkdownReport()
    report.heading(3, "2. 分模块独立数据表")

    test_names = [
        ("file_generation", "表1：文件生成性能对比表"),
        ("zip_compression", "表2：多线程压缩性能对比表"),
        ("file_copy", "表3：多线程复制性能对比表"),
        ("zip_extract", "表4：多线程解压性能对比表"),
    ]

    for key, title in test_names:
        report.heading(4, title)

        # 纵向表：每个系统内部看线程梯度
        for res in results:
            report.add(f"**{res.system_name} — 线程梯度：**")
            headers, rows = render_thread_table(res.raw_data["tests"][key])
            report.table(headers, rows)

        # 横向表：三系统同线程数对比（新增）
        report.add(f"**{title.split('：')[1]} — 三系统横向对比：**")
        report.add(render_cross_system_table(results, key))

    return report.build()


def generate_system_appendix(results: List[LoadedResult]) -> str:
    """生成附录中的系统信息部分."""
    report = MarkdownReport()
    report.heading(3, "1. 系统环境信息")

    for res in results:
        report.add(f"**{res.system_name}：**")
        info = res.raw_data["system_info"]
        report.code_block(
            "json", json.dumps(info, indent=2, ensure_ascii=False)
        )

    return report.build()


def generate_raw_data_appendix(results: List[LoadedResult]) -> str:
    """生成附录中的原始数据片段."""
    report = MarkdownReport()
    report.heading(3, "2. 完整原始数据（片段）")

    for res in results:
        report.add(f"**{res.system_name}：**")
        snippet = json.dumps(res.raw_data, indent=2, ensure_ascii=False)
        if len(snippet) > 3000:
            snippet = snippet[:3000] + "\n... [完整数据见 artifact JSON]"
        report.code_block("json", snippet)

    return report.build()


def generate_full_report(results: List[LoadedResult]) -> str:
    """组装完整 Markdown 报告."""
    report = MarkdownReport()

    # 标题
    report.heading(
        1, "基于 GitHub 虚拟机的多线程文件操作性能实验数据分析报告"
    )

    # 一、实验基本概况
    report.heading(2, "一、实验基本概况")
    report.heading(3, "1. 实验目的")
    report.add(
        "在 **GitHub Actions 官方虚拟机** 环境下，探究多线程对海量文本文件的 "
        "**生成、ZIP 压缩、文件复制、ZIP 解压** 四大操作的性能影响；"
        "对比不同系统虚拟机的运行差异，分析线程数、系统类型、文件结构"
        "对执行速度的影响规律。"
    )

    report.heading(3, "2. 统一实验参数（全环境一致）")
    report.add("- 总文件数量：10,000 个纯文本文件")
    report.add("- 文件总大小：严格 1 GiB（1,073,741,824 字节）")
    report.add("- 文件格式：`.txt`、`.py`、`.json`、`.md`、`.csv`、`.log` 随机生成")
    report.add("- 目录嵌套层数：随机 1~9 层")
    report.add("- 单文件大小范围：1 KiB ~ 1 MiB 随机分配")
    report.add("- 测试线程梯度：1、2、4、8、16 线程")
    report.add("- 运行环境：仅使用 GitHub Actions 内置虚拟机\n")

    report.heading(3, "3. 本次测试环境清单")
    for res in results:
        info = res.raw_data["system_info"]
        has_7z = "✅ 可用" if info.get("has_7zip") else "❌ 未安装（自动降级）"
        report.add(f"1. **{res.system_name}**")
        report.add(f"   - 系统: {info.get('system', 'N/A')} "
                   f"{info.get('release', '')}")
        report.add(f"   - 架构: {info.get('machine', 'N/A')}")
        report.add(f"   - Python: {info.get('python_version', 'N/A')}")
        report.add(f"   - 7-Zip: {has_7z}")
    report.add("")

    # 二、数据整理
    report.heading(2, "二、数据整理")
    report.add(generate_summary_table(results))
    report.add(generate_detail_tables(results))

    # 三、数据分析（框架）
    report.heading(2, "三、数据分析维度")
    report.add("> 以下分析框架需根据实际运行数据填充具体数值和结论。\n")

    report.heading(3, "（一）线程数量对性能的影响分析")
    report.heading(4, "1. 压缩操作（CPU 密集型）")
    report.add("- **趋势观察**：线程数从 1→16 提升时...")
    report.add("- **性能拐点**：...")
    report.add("- **最优线程数**：...")
    report.add("- **瓶颈原因**：GitHub 虚拟机 vCPU 核心数、GIL、"
                 "内存带宽竞争...\n")

    report.heading(4, "2. 复制、解压操作（IO 密集型）")
    report.add("- **速度提升幅度**：...")
    report.add("- **IO 瓶颈节点**：...")
    report.add("- **万级小文件局限**：大量 `open/close` 系统调用开销"
                 "抵消并行收益...\n")

    report.heading(3, "（二）GitHub 不同系统虚拟机横向对比分析")
    report.add("1. **整体性能排名**：Ubuntu > Windows > macOS"
                 "（以实际数据为准）")
    report.add("2. **分项对比**：生成 / 压缩 / 复制 / 解压")
    report.add("3. **差异原因**：ext4 vs NTFS vs APFS 的元数据操作"
                 "与 IO 调度策略\n")

    report.heading(3, "（三）四大文件操作类型对比分析")
    report.add("- **耗时排序**：压缩 > 解压 > 复制 > 生成（预估）")
    report.add("- **底层逻辑**：CPU 密集 vs IO 密集 vs 元数据密集")
    report.add("- **零散小文件损耗**：10,000 次系统调用开销显著\n")

    report.heading(3, "（四）工具依赖带来的性能差异分析")
    report.add("- **速度差距**：7-Zip 多线程通常比 Python zipfile "
                 "快 X~Y 倍")
    report.add("- **降级影响**：缺失 7-Zip 时自动降级单线程，"
                 "数据已单独标注\n")

    # 四、结论
    report.heading(2, "四、实验现象与最终结论")
    report.heading(3, "1. 通用规律结论")
    report.add("- CPU 密集型（压缩）：线程数提升至 vCPU 核心数附近"
                 "收益最大")
    report.add("- IO 密集型（复制/解压）：2~4 线程为甜蜜点")
    report.add("- 万级小文件：多线程优化被文件系统元数据操作稀释\n")

    report.heading(3, "2. 环境适配结论")
    report.add("- **Ubuntu**：综合最优，适合高并发文件操作")
    report.add("- **Windows**：NTFS 元数据较重，多线程扩展性受限")
    report.add("- **macOS**：APFS 稳定，压缩解压受工具链影响大\n")

    report.heading(3, "3. 实操建议结论")
    headers = ["场景", "推荐系统", "推荐线程数", "推荐工具"]
    rows = [
        ["批量压缩", "Ubuntu", "4~8", "7-Zip (-mmt)"],
        ["批量解压", "Ubuntu", "4~8", "7-Zip (-mmt)"],
        ["文件复制/迁移", "Ubuntu", "2~4", "Python shutil"],
        ["文件生成", "Ubuntu", "4~8", "Python 多线程"],
        ["无 7-Zip 环境", "任意", "1（压缩/解压）", "Python 标准库"],
    ]
    report.table(headers, rows)

    report.heading(3, "4. 实验客观局限性")
    report.add("- GitHub Actions 为共享资源，存在邻居干扰")
    report.add("- 每次分配的物理核心数、磁盘类型不完全一致")
    report.add("- 后台服务（监控、日志）占用资源")
    report.add("- 磁盘缓存策略对重复测试有加速效应，本实验已做清理")
    report.add("- 网络存储 vs 本地存储的不确定性\n")

    # 五、拓展
    report.heading(2, "五、拓展探究思考")
    report.add("1. 目录嵌套层数多少会明显拖累速度？")
    report.add("2. 若修改单文件大小分布，结论是否会改变？")
    report.add("3. 不同镜像的 CPU 调度、磁盘 IO 策略如何影响效率？")
    report.add("4. 异步 IO (aiofiles)、内存映射等方案的优化潜力\n")

    # 六、附录
    report.heading(2, "六、附录")
    report.add(generate_system_appendix(results))
    report.add(generate_raw_data_appendix(results))

    report.heading(3, "3. 实验执行流程回顾")
    report.add("1. GitHub Actions 触发工作流，三台虚拟机并行启动")
    report.add("2. 安装 Python 3.12 与 7-Zip（如可用）")
    report.add("3. 执行 `benchmark.py`，依次完成生成、压缩、复制、"
                 "解压测试")
    report.add("4. 各虚拟机输出 `benchmark_{系统}.json`")
    report.add("5. `generate-report` job 合并结果，生成 Markdown 报告")
    report.add("6. 报告与原始数据作为 artifact 上传")

    return report.build()


# =============================================================================
# 主入口
# =============================================================================

def main() -> None:
    """加载数据并生成报告."""
    results = load_all_results()

    if not results:
        print("错误：未找到实验结果文件！请确认 artifacts 目录存在。")
        raise SystemExit(1)

    report_text = generate_full_report(results)

    output_dir = Path("final-report")
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / "analysis_report.md"

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report_text)

    print(f"报告已生成: {output_file}")


if __name__ == "__main__":
    main()