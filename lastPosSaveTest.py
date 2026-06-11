import os
import json


# POSITION_PATH = os.path.abspath(
#     os.path.join(
#         os.path.dirname(__file__),
#         "..",
#         "..",
#         "config",
#         "last_positions.json"
#     )
# )

# print("Resolved path:", POSITION_PATH)
# print("Exists:", os.path.exists(POSITION_PATH))

POSITION_PATH = "galil_controller_suite/config/last_positions.json"

with open(POSITION_PATH, "r") as f:
    data = json.load(f)
    print(data)
    data["A"]["last_position"] = 32
    data["A"]["last_direction"] = 44

with open(POSITION_PATH, "w") as f:
    json.dump(data, f, indent=2)