"""MinerU PDF 转 Markdown 封装（Standard API，批量上传）。

批量流程（比单文件快很多）：
  1. POST /api/v4/file-urls/batch  申请一批上传地址（拿到 batch_id + file_urls）
  2. PUT file_urls[i]              逐个上传 PDF（注意：不能带 Authorization，预签名 URL 已带签名）
  3. GET /api/v4/extract-results/batch/{batch_id}  轮询，直到所有文件 state 都结束
  4. 下载 full_zip_url → 解压取 full.md

需要环境变量 MINERU_API_TOKEN（Standard API 的 Bearer token）。
"""
import io
import os
import time
import zipfile

import requests

BATCH_URL = "https://mineru.net/api/v4/file-urls/batch"
RESULT_URL = "https://mineru.net/api/v4/extract-results/batch/{batch_id}"


def _get_token() -> str:
    """从环境变量读取 MinerU token（自动加载 .env）。"""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    tok = os.getenv("MINERU_API_TOKEN")
    if not tok:
        raise RuntimeError(
            "缺少 MINERU_API_TOKEN，请在项目 .env 中配置：MINERU_API_TOKEN=你的token"
        )
    return tok


def _auth_headers(token: str) -> dict:
    return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}


def _validate_pdfs(pdf_paths) -> list:
    """校验并返回规范化后的绝对路径列表。"""
    pdf_paths = [os.path.abspath(p) for p in pdf_paths]
    for p in pdf_paths:
        if not os.path.isfile(p):
            raise FileNotFoundError(f"PDF 不存在: {p}")
        if not p.lower().endswith(".pdf"):
            raise ValueError(f"不是 PDF 文件: {p}")
    return pdf_paths


def _upload_and_poll(pdf_paths, token, model_version, poll_interval, timeout):
    """申请批量上传地址 → 逐个上传 → 轮询直到全部结束。返回 extract_result 列表。"""
    files = [
        {"name": os.path.basename(p), "data_id": str(i)}
        for i, p in enumerate(pdf_paths)
    ]
    resp = requests.post(
        BATCH_URL,
        headers=_auth_headers(token),
        json={"files": files, "model_version": model_version},
        timeout=60,
    )
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"MinerU 申请上传地址失败: {body.get('msg')} {body}")

    batch_id = body["data"]["batch_id"]
    file_urls = body["data"]["file_urls"]
    print(f"[MinerU] 批量任务创建 batch_id={batch_id}，共 {len(file_urls)} 个文件")

    for p, url in zip(pdf_paths, file_urls):
        with open(p, "rb") as f:
            r = requests.put(url, data=f, timeout=600)
        if r.status_code >= 400:
            raise RuntimeError(f"MinerU 上传失败 {os.path.basename(p)}: HTTP {r.status_code}")
        print(f"  [MinerU] 上传成功: {os.path.basename(p)}")

    items = None
    start = time.time()
    while time.time() - start < timeout:
        q = requests.get(
            RESULT_URL.format(batch_id=batch_id),
            headers=_auth_headers(token),
            timeout=60,
        ).json()
        data = q.get("data", {}) or {}
        items = data.get("extract_result", [])
        if items and all(it["state"] in ("done", "failed") for it in items):
            break
        for it in items:
            if it["state"] == "running":
                prog = it.get("extract_progress") or {}
                print(
                    f"    {it['file_name']}: {prog.get('extracted_pages', '?')}/{prog.get('total_pages', '?')} 页"
                )
        time.sleep(poll_interval)

    if not items:
        raise TimeoutError(f"MinerU 解析超时（>{timeout}s）")
    return items


def _safe_extract_all(zf: zipfile.ZipFile, dest_dir: str) -> list:
    """完整解压 zip 到 dest_dir（保留 zip 内目录结构），防 zip slip。返回解压出的文件路径列表。"""
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.abspath(dest_dir)
    extracted = []
    for member in zf.namelist():
        target = os.path.abspath(os.path.join(base, member))
        # 防 zip slip：跳过逃逸出 dest_dir 的路径
        if target != base and not target.startswith(base + os.sep):
            print(f"  [MinerU] 跳过可疑路径: {member}")
            continue
        if member.endswith("/"):
            continue  # 目录条目，extract 文件时会自动创建父目录
        zf.extract(member, base)
        extracted.append(target)
    return extracted


def pdfs_to_markdown(
    pdf_paths,
    model_version: str = "vlm",
    poll_interval: int = 5,
    timeout: int = 1800,
) -> dict:
    """批量把多个 PDF 转成 markdown。

    参数:
        pdf_paths: PDF 文件路径列表
        model_version: vlm（推荐，学术论文质量好）/ pipeline / MinerU-HTML
        poll_interval: 轮询间隔（秒）
        timeout: 总超时（秒）

    返回:
        {文件名: markdown 文本}，解析失败的项值为 None
    """
    pdf_paths = _validate_pdfs(pdf_paths)
    token = _get_token()

    # 1-3. 申请上传地址 → 上传 → 轮询
    items = _upload_and_poll(pdf_paths, token, model_version, poll_interval, timeout)

    # 4. 下载并解压每个文件的结果（只取 full.md）
    out = {}
    for it in items:
        name = it["file_name"]
        if it["state"] != "done":
            print(f"  [MinerU] 解析失败: {name} -> {it.get('err_msg')}")
            out[name] = None
            continue
        zip_resp = requests.get(it["full_zip_url"], timeout=600)
        if zip_resp.status_code >= 400:
            print(f"  [MinerU] 下载失败: {name} -> HTTP {zip_resp.status_code}")
            out[name] = None
            continue
        with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
            md_name = next(
                (n for n in zf.namelist() if n.endswith("full.md")), None
            )
            if md_name is None:
                print(f"  [MinerU] zip 里没找到 full.md: {name}")
                out[name] = None
                continue
            md = zf.read(md_name).decode("utf-8", errors="replace")
            out[name] = md
            print(f"  [MinerU] 完成: {name} -> markdown {len(md)} 字符")

    return out


def pdf_to_markdown(pdf_path, **kwargs) -> str:
    """单文件便捷包装（内部走批量，一次一个）。"""
    res = pdfs_to_markdown([pdf_path], **kwargs)
    name = os.path.basename(os.path.abspath(pdf_path))
    return res.get(name)


def pdfs_to_markdown_files(
    pdf_paths, output_dir: str, **kwargs
) -> dict:
    """批量转 markdown 并写入 output_dir（同名 .md 文件）。

    返回 {pdf文件名: md文件路径}。
    """
    os.makedirs(output_dir, exist_ok=True)
    result = pdfs_to_markdown(pdf_paths, **kwargs)
    out = {}
    for name, md in result.items():
        if md is None:
            out[name] = None
            continue
        base = os.path.splitext(name)[0]
        md_path = os.path.join(output_dir, base + ".md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md)
        out[name] = md_path
    return out


def folder_to_markdown(folder: str, output_dir: str = None, **kwargs) -> dict:
    """扫描文件夹里所有 PDF，批量转成 md 文件。

    参数:
        folder: 存放 PDF 的文件夹（递归扫描子目录）
        output_dir: md 输出目录（默认 folder 下的 md 子目录）
        其余参数透传给 pdfs_to_markdown

    返回:
        {PDF文件名: md文件路径}，解析失败值为 None
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"文件夹不存在: {folder}")

    pdf_paths = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".pdf"):
                pdf_paths.append(os.path.join(root, f))
    pdf_paths.sort()

    if not pdf_paths:
        raise FileNotFoundError(f"文件夹里没有 PDF: {folder}")
    print(f"[MinerU] 找到 {len(pdf_paths)} 个 PDF")

    if output_dir is None:
        output_dir = os.path.join(folder, "md")

    return pdfs_to_markdown_files(pdf_paths, output_dir, **kwargs)


def pdfs_extract_all(
    pdf_paths,
    output_dir: str,
    model_version: str = "vlm",
    poll_interval: int = 5,
    timeout: int = 1800,
) -> dict:
    """批量把 PDF 转成 markdown，并**完整保留** MinerU 返回的所有内容。

    与 pdfs_to_markdown 只取 full.md 不同：这里把每个 PDF 的结果 zip 完整下载到磁盘，
    解压成一个一个文件（full.md + 图片 + 其他资源），再删掉 zip。输出结构：

        output_dir/<PDF名去后缀>/full.md
        output_dir/<PDF名去后缀>/images/...

    参数同 pdfs_to_markdown，另加 output_dir（必填，解压根目录）。

    返回:
        {PDF文件名: 解压目录路径}，解析/下载失败的值为 None
    """
    pdf_paths = _validate_pdfs(pdf_paths)
    token = _get_token()

    items = _upload_and_poll(pdf_paths, token, model_version, poll_interval, timeout)

    os.makedirs(output_dir, exist_ok=True)
    out = {}
    for it in items:
        name = it["file_name"]
        stem = os.path.splitext(name)[0]
        dest_dir = os.path.join(output_dir, stem)
        zip_path = os.path.join(dest_dir, "_tmp.zip")

        if it["state"] != "done":
            print(f"  [MinerU] 解析失败: {name} -> {it.get('err_msg')}")
            out[name] = None
            continue

        try:
            os.makedirs(dest_dir, exist_ok=True)
            r = requests.get(it["full_zip_url"], timeout=600)
            if r.status_code >= 400:
                print(f"  [MinerU] 下载失败: {name} -> HTTP {r.status_code}")
                out[name] = None
                continue
            with open(zip_path, "wb") as f:
                f.write(r.content)
            with zipfile.ZipFile(zip_path) as zf:
                extracted = _safe_extract_all(zf, dest_dir)
            print(f"  [MinerU] 完成: {name} -> {dest_dir}（{len(extracted)} 个文件）")
            out[name] = dest_dir
        except Exception as e:
            print(f"  [MinerU] 解压失败: {name} -> {e}")
            out[name] = None
        finally:
            # 删除压缩包，只留解压出来的文件
            if os.path.exists(zip_path):
                os.remove(zip_path)

    return out


def folder_extract_all(folder: str, output_dir: str = None, **kwargs) -> dict:
    """扫描文件夹里所有 PDF，完整解压转成 markdown（含图片等全部内容）。

    参数:
        folder: 存放 PDF 的文件夹（递归扫描子目录）
        output_dir: 解压根目录（默认 folder 下的 md 子目录）
        其余参数透传给 pdfs_extract_all

    返回:
        {PDF文件名: 解压目录路径}，解析失败值为 None
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder):
        raise NotADirectoryError(f"文件夹不存在: {folder}")

    pdf_paths = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".pdf"):
                pdf_paths.append(os.path.join(root, f))
    pdf_paths.sort()

    if not pdf_paths:
        raise FileNotFoundError(f"文件夹里没有 PDF: {folder}")
    print(f"[MinerU] 找到 {len(pdf_paths)} 个 PDF")

    if output_dir is None:
        output_dir = os.path.join(folder, "md")

    return pdfs_extract_all(pdf_paths, output_dir, **kwargs)


# if __name__ == "__main__":
#     import sys
#
#     if len(sys.argv) < 2:
#         print("用法:")
#         print("  传文件夹（递归转里面所有 PDF）: python mineru_pdf.py <文件夹> [输出目录]")
#         print("  传文件（多个）:               python mineru_pdf.py <pdf1> <pdf2> ...")
#         sys.exit(1)
#
#     # 如果第一个参数是目录，走文件夹批量模式
#     if os.path.isdir(sys.argv[1]):
#         out = sys.argv[2] if len(sys.argv) > 2 else None
#         result = folder_to_markdown(sys.argv[1], out)
#         for name, path in result.items():
#             print(f"\n{name} -> {path}")
#     else:
#         result = pdfs_to_markdown(sys.argv[1:])
#         for name, md in result.items():
#             print("\n" + "=" * 40 + f" {name} " + "=" * 40)
#             if md:
#                 print(md[:2000])
#             else:
#                 print("（解析失败）")


# result = folder_to_markdown(r"D:/papers")