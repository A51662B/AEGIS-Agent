from tools.Toolregistry import tool


@tool(name="reportWorkflow", description="这是一个工作流，可以用于pdf论文的批量报告编写，是一个科研小工具。"
                                         "第一个参数pdf_paths代表pdf论文所在的目录，请传递绝对路径（必填），"
                                         "当解析完成时会发送解析成功后的文件地址通知，届时你将可以更具地址找到解析好的文件，不允许频繁调用，除非收到通知表示产生错误"
                                         # "第二个参数output_dir代表输出md论文报告的路径,，（建议只选择与pdf报告相同目录）（可选，不传则默认输出到论文目录下的reports.md）"
                                         "不允许一直轮询文件夹，任务完成会发送通知，收到通知之后再检查文件夹")
def reportWorkflow(pdf_paths: str) -> str:
    output_dir = None
    """同步执行论文报告工作流（由 ToolProxy 用 to_thread 丢线程池跑，不阻塞事件循环）。"""
    from workflow.report_workflow import run
    final = run(pdf_paths, output_dir, auto=True)
    return "完成！最终输出: " + str(final.get("output_path"))
