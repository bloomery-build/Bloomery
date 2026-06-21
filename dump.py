import ini, json

print(json.dumps(ini.parse(open('test.ini').read()), indent=4))