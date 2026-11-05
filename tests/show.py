import sys, json, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import dynamic_buffer as db
s = db.infer(sys.argv[1])
print(json.dumps(s["filter"])); [print(json.dumps(c)) for c in s["columns"]]
