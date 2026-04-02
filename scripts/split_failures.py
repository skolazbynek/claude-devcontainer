import re
import os

with open("test_output.txt") as f:
    content = f.read()

# Find the FAILURES section
failures_start = content.find("=================================== FAILURES ===================================")
if failures_start == -1:
    print("No FAILURES section found")
    exit(1)

failures_section = content[failures_start:]

# Split on failure block headers: lines like "_ TestName _"
header_pattern = re.compile(r'^(_ .+ _)$', re.MULTILINE)
headers = list(header_pattern.finditer(failures_section))

os.makedirs("test_failures", exist_ok=True)

for i, match in enumerate(headers):
    test_name_raw = match.group(1)[2:-2]  # strip leading "_ " and trailing " _"
    # Determine block content: from this header to next header (or end of section)
    block_start = match.start()
    block_end = headers[i + 1].start() if i + 1 < len(headers) else len(failures_section)
    block = failures_section[block_start:block_end].rstrip()

    # Sanitize test name for filename
    safe_name = re.sub(r'[^\w\-.]', '_', test_name_raw)
    safe_name = re.sub(r'_+', '_', safe_name).strip('_')
    filename = f"test_failures/{safe_name}.txt"

    with open(filename, 'w') as f:
        f.write(block + "\n")
    print(f"Written: {filename}")
