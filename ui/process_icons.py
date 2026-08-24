import os
from PIL import Image

def remove_white_background(img_path, out_path):
    print(f"Processing {img_path}...")
    try:
        img = Image.open(img_path).convert("RGBA")
        datas = img.getdata()

        newData = []
        for item in datas:
            # Change all white (also shades of whites)
            # to transparent
            if item[0] > 240 and item[1] > 240 and item[2] > 240:
                newData.append((255, 255, 255, 0))
            else:
                newData.append(item)

        img.putdata(newData)
        # Crop the image to its bounding box
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        
        # Resize to max 64x64 while maintaining aspect ratio
        img.thumbnail((64, 64), Image.Resampling.LANCZOS)
        
        img.save(out_path, "PNG")
        print(f"Saved to {out_path}")
    except Exception as e:
        print(f"Error processing {img_path}: {e}")

input_dir = r"c:\Users\Bussiness Sensor\Desktop\10alytics_project\ui\public\integrations"
for filename in ["gmail.jpg", "tavily.png", "linkup.png", "slack.png", "exa.png"]:
    in_path = os.path.join(input_dir, filename)
    out_path = os.path.join(input_dir, filename.split('.')[0] + "_transparent.png")
    remove_white_background(in_path, out_path)
