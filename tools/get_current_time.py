from datetime import datetime

from tools.Toolregistry import tool


@tool(name="get_current_time", description="获取当前的日期、时间和星期，用于回答与「现在、今天、当前时间」相关的问题")
def get_current_time():
    now = datetime.now()
    weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"星期：{weekdays[now.weekday()]}"
    )
