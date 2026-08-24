import re, base64, os

html = open("original_icons.html", "r", encoding="utf-8").read()

# Extract all icon-badge image srcs
pattern = r'<div class="icon-badge"><img src="(data:image/png;base64,[^"]+)" alt="([^"]+)"'
matches = re.findall(pattern, html)

out_dir = r"c:\Users\Bussiness Sensor\Desktop\10alytics_project\ui\public\integrations"
os.makedirs(out_dir, exist_ok=True)

seen = set()
for src, alt in matches:
    name = alt.lower()
    if name in seen:
        continue
    seen.add(name)
    b64data = src.split(",", 1)[1]
    img_bytes = base64.b64decode(b64data)
    out_path = os.path.join(out_dir, f"{name}.png")
    with open(out_path, "wb") as f:
        f.write(img_bytes)
    print(f"Saved {name}.png ({len(img_bytes)} bytes)")

print("Done. Icons found:", list(seen))
