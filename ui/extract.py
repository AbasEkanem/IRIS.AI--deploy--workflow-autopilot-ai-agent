import re

log_path = r'C:\Users\Bussiness Sensor\.gemini\antigravity\brain\54d76881-6e66-469d-aa5d-5a5432b37b83\.system_generated\logs\overview.txt'

try:
    with open(log_path, 'r', encoding='utf-8') as f:
        content = f.read()
except FileNotFoundError:
    print("Log file not found.")
    exit(1)

# Find the marquee-wrap div
match = re.search(r'<div class="marquee-wrap">(.*?)</div>\s*</div>', content, re.DOTALL)
if not match:
    print("Could not find marquee-wrap in log.")
    exit(1)

marquee_html = match.group(1)

# Convert class to className
marquee_html = marquee_html.replace('class=', 'className=')

# Also the image tags might not be closed properly for JSX (e.g. <img> instead of <img />)
# The user's prompt had <img src="..." alt="..." title="..."/> which is valid JSX if closed.
marquee_html = marquee_html.replace('"></div>', '"/></div>').replace('/>"/>', '/>')

# The user prompt had:
# <div class="marquee-track"><div class="icon-badge"><img src="..." alt="Slack" title="Slack"/></div>...</div>

component_code = f"""import React from 'react';

export default function IntegrationMarquee() {{
  return (
    <>
      <style>{{`
        .marquee-wrap {{
          width: 480px;
          max-width: 100%;
          flex: 0 0 auto;
          margin: 0 auto;
          overflow: hidden;
          -webkit-mask-image: linear-gradient(to right, transparent 0%, black 12%, black 88%, transparent 100%);
          mask-image: linear-gradient(to right, transparent 0%, black 12%, black 88%, transparent 100%);
        }}
        .marquee-track {{
          display: flex;
          width: max-content;
          gap: 34px;
          animation: scroll 18s linear infinite;
          will-change: transform;
          backface-visibility: hidden;
          transform: translate3d(0, 0, 0);
        }}
        @keyframes scroll {{
          from {{ transform: translate3d(0, 0, 0); }}
          to {{ transform: translate3d(-50%, 0, 0); }}
        }}
        .icon-badge {{
          flex: 0 0 auto;
          width: 42px;
          height: 42px;
          display: flex;
          align-items: center;
          justify-content: center;
          background: var(--chipBg);
          border: 1px solid var(--chipBorder);
          border-radius: 12px;
          transition: transform 0.25s ease, opacity 0.25s ease, background 0.25s ease;
          opacity: 0.92;
          backface-visibility: hidden;
          padding: 8px;
        }}
        .icon-badge:hover {{
          transform: translateY(-3px) scale(1.12);
          opacity: 1;
          background: var(--inputBg);
        }}
        .icon-badge img {{
          width: 100%;
          height: 100%;
          object-fit: contain;
          border-radius: 6px;
        }}
      `}}</style>
      <div className="marquee-wrap">
        <div className="marquee-track">
          {{/* Render twice for seamless infinite scrolling */}}
          <React.Fragment>
            {marquee_html}
          </React.Fragment>
          <React.Fragment>
            {marquee_html}
          </React.Fragment>
        </div>
      </div>
    </>
  );
}}
"""

out_path = r'c:\Users\Bussiness Sensor\Desktop\10alytics_project\ui\src\components\IntegrationMarquee.tsx'
with open(out_path, 'w', encoding='utf-8') as f:
    f.write(component_code)

print("Successfully wrote IntegrationMarquee.tsx")
