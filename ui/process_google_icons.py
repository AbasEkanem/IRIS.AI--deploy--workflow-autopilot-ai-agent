import os
from PIL import Image

def remove_white_better(img_path, out_path):
    print(f"Processing {img_path}...")
    try:
        img = Image.open(img_path).convert("RGBA")
        datas = img.getdata()

        newData = []
        for item in datas:
            R, G, B = item[0], item[1], item[2]
            A = item[3] if len(item) > 3 else 255

            brightness = (R + G + B) / 3
            saturation_proxy = max(R, G, B) - min(R, G, B)

            if brightness > 210 and saturation_proxy < 30:
                # Whitish/greyish background pixel -> fade to transparent
                new_alpha = int(255 * (255 - brightness) / 45)
                new_alpha = max(0, min(A, new_alpha))
                newData.append((R, G, B, new_alpha))
            else:
                newData.append((R, G, B, A))

        img.putdata(newData)

        # Crop to content
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)

        # Pad to square (transparent)
        max_dim = max(img.size)
        square_img = Image.new('RGBA', (max_dim, max_dim), (255, 255, 255, 0))
        offset = ((max_dim - img.width) // 2, (max_dim - img.height) // 2)
        square_img.paste(img, offset)

        # Resize uniformly to 128x128
        square_img = square_img.resize((128, 128), Image.Resampling.LANCZOS)

        square_img.save(out_path, "PNG")
        print(f"Saved to {out_path}")
    except Exception as e:
        print(f"Error processing {img_path}: {e}")

input_dir = r"c:\Users\Bussiness Sensor\Desktop\10alytics_project\ui\public\integrations"

# Original uploaded files (with spaces) -> clean transparent PNG outputs
originals = {
    "gcal_clean":       "google calendar icon.jpg",
    "gclassroom_clean": "google class room icon.jpg",
    "gdocs_clean":      "google doc icon.png",
    "gdrive_clean":     "google drive icon.png",
    "gforms_clean":     "google forms icon.png",
    "gslides_clean":    "google slides icon.png",
}

for out_name, src_name in originals.items():
    src_path = os.path.join(input_dir, src_name)
    out_path = os.path.join(input_dir, f"{out_name}.png")
    remove_white_better(src_path, out_path)
