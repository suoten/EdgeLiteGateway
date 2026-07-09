import sys, os

path = sys.argv[1]
offset = int(sys.argv[2])
limit = int(sys.argv[3])
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()
for i in range(offset, min(offset+limit, len(lines))):
    print(f"{i+1}\t{lines[i].rstrip('\n')}")
