from tools.Toolregistry import tool


@tool(name="pdfToMarkdown", description="这是一个工作流，可以把指定目录下的 PDF 论文批量转换为 Markdown 文件（只转换，不写报告），"
                                       "并完整保留解析出的所有内容（markdown 正文 + 图片等资源）。"
                                       "第一个参数 pdf_paths 代表存放 PDF 论文的目录，请传递绝对路径（必填）。"
                                       "第二个参数 output_dir 代表转换结果的输出目录（可选）："
                                       "传绝对路径则直接作为输出目录；传相对路径则相对于 pdf_paths 目录；不传则默认输出到 PDF 目录下的 md 子目录")
def pdfToMarkdown(pdf_paths: str, output_dir: str = None) -> str:
    """同步执行 PDF 转 Markdown 工作流（由 ToolProxy 用 to_thread 丢线程池跑，不阻塞事件循环）。"""
    from workflow.pdf_to_md_workflow import run
    final = run(pdf_paths, output_dir, auto=True)
    return "完成！输出目录: " + str(final.get("output_dir"))
