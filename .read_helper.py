import sys

path = sys.argv[1]
offset = int(sys.argv[2])
limit = int(sys.argv[3])
with open(path, encoding='utf-8') as f:
    lines = f.readlines()
for i in range(offset, min(offset+limit, len(lines))):
    line = lines[i].rstrip('\n')
    print(f"{i+1}\t{line}")
