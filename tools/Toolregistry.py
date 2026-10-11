import inspect
import typing

class Toolregistry():
    def __init__(self):
        self._toolDescription = []
        self._toolMap = {}

    def getToolDescription(self):
        return self._toolDescription

    def getToolMap(self):
        return self._toolMap

    def register(self, func, name, description):
        properties = {}
        required = []

        typeSwitch = {
            int: "integer",
            str: "string",
            float: "number",
            bool: "boolean",
        }

        sig = inspect.signature(func)
        # hints = typing.get_type_hints(func)
        for paramname, param in sig.parameters.items():
            parameterType = typeSwitch.get(param.annotation, "string")
            properties[param.name] = {
                        "type": parameterType,
                        "description": ""
                    }
            if param.default == inspect.Parameter.empty:
                required.append(paramname)

        parameters = {
                "type": "object",
                "properties": properties,
                "required": required
            }


        des = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters
            }
        }

        self._toolDescription.append(des)
        self._toolMap[name] = func



toolregistry = Toolregistry()


def tool(name,description):
    def decorator(func):
        toolregistry.register(func, name, description)
        return func

    return decorator



