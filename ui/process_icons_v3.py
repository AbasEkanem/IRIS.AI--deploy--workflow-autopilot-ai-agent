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
            
            # Simple Luma Key
            # If the pixel is very bright and has low saturation (grey/white)
            brightness = (R + G + B) / 3
            
            # Difference between max and min channel is a proxy for saturation
            saturation_proxy = max(R, G, B) - min(R, G, B)
            
            if brightness > 210 and saturation_proxy < 30:
                # It's a whitish/greyish background pixel
                # Map brightness 210->255 to alpha 255->0
                new_alpha = int(255 * (255 - brightness) / 45)
                new_alpha = max(0, min(A, new_alpha))
                # We can also pull the RGB down slightly to avoid white halos on dark backgrounds
                newData.append((R, G, B, new_alpha))
            else:
                newData.append((R, G, B, A))

        img.putdata(newData)
        
        # Crop
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
            
        # Pad to square
        max_dim = max(img.size)
        square_img = Image.new('RGBA', (max_dim, max_dim), (255, 255, 255, 0))
        offset = ((max_dim - img.width) // 2, (max_dim - img.height) // 2)
        square_img.paste(img, offset)
        
        # Resize uniformly
        square_img = square_img.resize((128, 128), Image.Resampling.LANCZOS)
        
        square_img.save(out_path, "PNG")
        print(f"Saved to {out_path}")
    except Exception as e:
        print(f"Error processing {img_path}: {e}")

input_dir = r"c:\Users\Bussiness Sensor\Desktop\10alytics_project\ui\public\integrations"
originals = {
    "gmail": r"C:\Users\Bussiness Sensor\.gemini\antigravity\brain\54d76881-6e66-469d-aa5d-5a5432b37b83\media__1782860407299.jpg",
    "tavily": r"C:\Users\Bussiness Sensor\.gemini\antigravity\brain\54d76881-6e66-469d-aa5d-5a5432b37b83\media__1782860407392.png",
    "linkup": r"C:\Users\Bussiness Sensor\.gemini\antigravity\brain\54d76881-6e66-469d-aa5d-5a5432b37b83\media__1782860407407.png",
    "slack": r"C:\Users\Bussiness Sensor\.gemini\antigravity\brain\54d76881-6e66-469d-aa5d-5a5432b37b83\media__1782860784326.png",
    "exa": r"C:\Users\Bussiness Sensor\.gemini\antigravity\brain\54d76881-6e66-469d-aa5d-5a5432b37b83\media__1782860616268.png"
}

for name, path in originals.items():
    out_path = os.path.join(input_dir, f"{name}_transparent.png")
    remove_white_better(path, out_path)
