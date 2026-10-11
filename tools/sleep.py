from tools.Toolregistry import *
import time

@tool(name="sleep", description="这是一个睡觉工具，没什么特别的，就是睡觉")
def sleep():
    time.sleep(7)
    return "睡醒了"
