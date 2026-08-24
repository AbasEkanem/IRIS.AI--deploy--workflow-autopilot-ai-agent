"use client";
import React from "react";
import Image from "next/image";

interface IrisLogoProps {
  size?: number;
  className?: string;
  style?: React.CSSProperties;
}

export default function IrisLogo({ size = 46, className = "", style = {} }: IrisLogoProps) {
  return (
    <div
      className={className}
      style={{
        width: size,
        height: size,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexShrink: 0,
        background: "transparent",
        ...style,
      }}
    >
      <Image
        src="/iris-logo.png"
        alt="IRIS"
        width={size}
        height={size}
        priority
        style={{
          width: size,
          height: size,
          objectFit: "contain",
          transform: "scale(1.05)",
        }}
      />
    </div>
  );
}
