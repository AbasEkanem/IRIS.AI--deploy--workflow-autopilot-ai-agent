import os
import io
from PIL import Image

def remove_background_better(img_path, out_path):
    print(f"Processing {img_path}...")
    try:
        # We will use rembg if available
        try:
            from rembg import remove
            with open(img_path, 'rb') as i:
                input_data = i.read()
            output_data = remove(input_data)
            img = Image.open(io.BytesIO(output_data)).convert("RGBA")
        except ImportError:
            print("rembg not installed, falling back to basic floodfill")
            img = Image.open(img_path).convert("RGBA")
            width, height = img.size
            pixels = img.load()
            visited = set()
            queue = []
            def is_white(c):
                return c[0] > 230 and c[1] > 230 and c[2] > 230
            for x in range(width):
                if is_white(pixels[x, 0]): queue.append((x, 0))
                if is_white(pixels[x, height-1]): queue.append((x, height-1))
            for y in range(height):
                if is_white(pixels[0, y]): queue.append((0, y))
                if is_white(pixels[width-1, y]): queue.append((width-1, y))
            for p in queue:
                visited.add(p)
            idx = 0
            while idx < len(queue):
                x, y = queue[idx]
                idx += 1
                for dx, dy in [(-1,0), (1,0), (0,-1), (0,1)]:
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < width and 0 <= ny < height:
                        if (nx, ny) not in visited:
                            visited.add((nx, ny))
                            if is_white(pixels[nx, ny]):
                                queue.append((nx, ny))
            for x, y in queue:
                pixels[x, y] = (255, 255, 255, 0)

        # Make them square
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        
        max_dim = max(img.size)
        square_img = Image.new('RGBA', (max_dim, max_dim), (255, 255, 255, 0))
        offset = ((max_dim - img.width) // 2, (max_dim - img.height) // 2)
        square_img.paste(img, offset)
        
        # Resize all uniformly
        square_img = square_img.resize((128, 128), Image.Resampling.LANCZOS)
        
        square_img.save(out_path, "PNG")
        print(f"Saved to {out_path}")
    except Exception as e:
        print(f"Error processing {img_path}: {e}")

input_dir = r"c:\Users\Bussiness Sensor\Desktop\10alytics_project\ui\public\integrations"
for filename in ["gmail.jpg", "tavily.png", "linkup.png", "slack.png", "exa.png"]:
    in_path = os.path.join(input_dir, filename)
    out_path = os.path.join(input_dir, filename.split('.')[0] + "_transparent.png")
    remove_background_better(in_path, out_path)
