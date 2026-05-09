"use client";

import { Billboard, Float, Html, Line, OrbitControls, RoundedBox, Stars } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";

type Platform = {
  id: string;
  label: string;
  url: string;
  initial: string;
  color: string;
  emissive: string;
  position: [number, number, number];
  phase: number;
};

const PLATFORMS: Platform[] = [
  {
    id: "reddit",
    label: "Reddit",
    url: "reddit.com",
    initial: "R",
    color: "#ff4500",
    emissive: "#ff7849",
    position: [-3.6, 0.6, 0.3],
    phase: 0,
  },
  {
    id: "x",
    label: "X",
    url: "x.com",
    initial: "𝕏",
    color: "#ffffff",
    emissive: "#cbd5e1",
    position: [0, -1.4, 1.6],
    phase: 1.7,
  },
  {
    id: "linkedin",
    label: "LinkedIn",
    url: "linkedin.com",
    initial: "in",
    color: "#0a66c2",
    emissive: "#3b82f6",
    position: [3.6, 0.6, 0.3],
    phase: 3.4,
  },
];

function CoreNode() {
  const meshRef = useRef<THREE.Mesh>(null);
  const innerRef = useRef<THREE.Mesh>(null);

  useFrame((_, delta) => {
    if (meshRef.current) {
      meshRef.current.rotation.x += delta * 0.15;
      meshRef.current.rotation.y += delta * 0.25;
    }
    if (innerRef.current) {
      innerRef.current.rotation.x -= delta * 0.4;
      innerRef.current.rotation.z += delta * 0.3;
    }
  });

  return (
    <Float speed={1.2} rotationIntensity={0} floatIntensity={0.25}>
      <group>
        <mesh ref={meshRef}>
          <icosahedronGeometry args={[0.95, 1]} />
          <meshStandardMaterial
            color="#1e1b4b"
            emissive="#8b5cf6"
            emissiveIntensity={0.6}
            metalness={0.7}
            roughness={0.2}
            wireframe
          />
        </mesh>
        <mesh ref={innerRef}>
          <icosahedronGeometry args={[0.55, 0]} />
          <meshStandardMaterial
            color="#a78bfa"
            emissive="#c4b5fd"
            emissiveIntensity={1.3}
            metalness={0.9}
            roughness={0.1}
          />
        </mesh>
        <pointLight color="#a78bfa" intensity={2} distance={8} />
        <Html center distanceFactor={10} position={[0, -1.5, 0]}>
          <div className="pointer-events-none whitespace-nowrap text-center font-mono text-[10px] text-violet-200/80 tracking-[0.25em] uppercase">
            Orchestrator
          </div>
        </Html>
      </group>
    </Float>
  );
}

function BrowserWindow({ platform }: { platform: Platform }) {
  const groupRef = useRef<THREE.Group>(null);

  useFrame((state) => {
    if (!groupRef.current) return;
    const t = state.clock.elapsedTime;
    groupRef.current.position.y =
      platform.position[1] + Math.sin(t * 0.7 + platform.phase) * 0.12;
    groupRef.current.position.x =
      platform.position[0] + Math.cos(t * 0.5 + platform.phase) * 0.08;
  });

  return (
    <Billboard
      follow
      lockX={false}
      lockY={false}
      lockZ={false}
      position={platform.position}
    >
      <group ref={groupRef}>
        <BrowserChrome platform={platform} />
      </group>
    </Billboard>
  );
}

function BrowserChrome({ platform }: { platform: Platform }) {
  const screenRef = useRef<THREE.Mesh>(null);

  useFrame((state) => {
    if (!screenRef.current) return;
    const mat = screenRef.current.material as THREE.MeshStandardMaterial;
    mat.emissiveIntensity =
      0.35 + Math.sin(state.clock.elapsedTime * 1.2 + platform.phase) * 0.08;
  });

  return (
    <group>
      {/* glow halo behind */}
      <mesh position={[0, 0, -0.04]}>
        <planeGeometry args={[2.2, 1.55]} />
        <meshBasicMaterial color={platform.emissive} transparent opacity={0.14} />
      </mesh>

      {/* body */}
      <RoundedBox args={[1.9, 1.25, 0.06]} radius={0.06} smoothness={4}>
        <meshStandardMaterial
          color="#0b0a18"
          metalness={0.6}
          roughness={0.35}
          emissive="#1a1530"
          emissiveIntensity={0.15}
        />
      </RoundedBox>

      {/* title bar */}
      <mesh position={[0, 0.52, 0.032]}>
        <planeGeometry args={[1.86, 0.18]} />
        <meshStandardMaterial color="#15132a" emissive="#15132a" emissiveIntensity={0.4} />
      </mesh>

      {/* traffic lights */}
      <TrafficLight position={[-0.82, 0.52, 0.034]} color="#ff5f57" />
      <TrafficLight position={[-0.74, 0.52, 0.034]} color="#febc2e" />
      <TrafficLight position={[-0.66, 0.52, 0.034]} color="#28c840" />

      {/* address pill */}
      <mesh position={[0.2, 0.52, 0.034]}>
        <planeGeometry args={[1, 0.1]} />
        <meshBasicMaterial color="#0a0915" transparent opacity={0.85} />
      </mesh>
      <Html
        center
        distanceFactor={3.5}
        position={[0.2, 0.52, 0.045]}
        occlude={false}
      >
        <div className="pointer-events-none whitespace-nowrap font-mono text-[8px] text-zinc-400 tracking-wider">
          {platform.url}
        </div>
      </Html>

      {/* screen content panel */}
      <mesh ref={screenRef} position={[0, -0.085, 0.032]}>
        <planeGeometry args={[1.86, 1.0]} />
        <meshStandardMaterial
          color={platform.color}
          emissive={platform.emissive}
          emissiveIntensity={0.4}
          metalness={0.2}
          roughness={0.5}
        />
      </mesh>

      {/* content rows simulating page content */}
      <ContentRows color={platform.emissive} />

      {/* big platform glyph */}
      <Html
        center
        distanceFactor={5}
        position={[0, -0.085, 0.05]}
        occlude={false}
      >
        <div
          className="pointer-events-none flex select-none flex-col items-center gap-1 font-bold text-white"
          style={{ textShadow: `0 0 20px ${platform.emissive}` }}
        >
          <span className="text-4xl leading-none">{platform.initial}</span>
          <span className="font-mono text-[9px] uppercase tracking-[0.3em] opacity-80">
            {platform.label}
          </span>
        </div>
      </Html>
    </group>
  );
}

function ContentRows({ color }: { color: string }) {
  const rows: { y: number; w: number }[] = [
    { y: -0.42, w: 1.6 },
    { y: -0.5, w: 1.2 },
  ];
  return (
    <>
      {rows.map((r) => (
        <mesh
          key={`row-${r.y.toFixed(2)}-${r.w.toFixed(2)}`}
          position={[(r.w - 1.7) / 2, r.y, 0.034]}
        >
          <planeGeometry args={[r.w, 0.04]} />
          <meshBasicMaterial color={color} transparent opacity={0.35} />
        </mesh>
      ))}
    </>
  );
}

function TrafficLight({
  position,
  color,
}: {
  position: [number, number, number];
  color: string;
}) {
  return (
    <mesh position={position}>
      <circleGeometry args={[0.025, 16]} />
      <meshStandardMaterial
        color={color}
        emissive={color}
        emissiveIntensity={0.7}
      />
    </mesh>
  );
}

function ConnectionLine({
  to,
  color,
  phase,
}: {
  to: [number, number, number];
  color: string;
  phase: number;
}) {
  const lineRef = useRef<THREE.Group>(null);

  useFrame((state) => {
    const line = lineRef.current?.children[0] as THREE.Line | undefined;
    const mat = line?.material as
      | (THREE.LineDashedMaterial & { dashOffset: number })
      | undefined;
    if (mat && "dashOffset" in mat) {
      mat.dashOffset = -state.clock.elapsedTime * 0.6 + phase;
    }
  });

  return (
    <group ref={lineRef}>
      <Line
        points={[
          [0, 0, 0],
          to,
        ]}
        color={color}
        lineWidth={1.5}
        transparent
        opacity={0.55}
        dashed
        dashSize={0.18}
        gapSize={0.12}
      />
    </group>
  );
}

export default function AgentScene() {
  return (
    <Canvas
      camera={{ position: [0, 1.5, 7], fov: 50 }}
      gl={{ antialias: true, alpha: true }}
      style={{ background: "transparent" }}
    >
      <color attach="background" args={["#05030f"]} />
      <fog attach="fog" args={["#05030f", 9, 20]} />

      <ambientLight intensity={0.2} />
      <directionalLight position={[5, 5, 5]} intensity={0.45} />
      <pointLight position={[-5, -3, -5]} color="#06b6d4" intensity={0.8} />
      <pointLight position={[5, 4, 4]} color="#a78bfa" intensity={0.5} />

      <Stars radius={50} depth={30} count={1200} factor={3} fade speed={0.4} />

      <CoreNode />

      {PLATFORMS.map((platform) => (
        <ConnectionLine
          key={`line-${platform.id}`}
          to={platform.position}
          color={platform.color}
          phase={platform.phase}
        />
      ))}

      {PLATFORMS.map((platform) => (
        <BrowserWindow key={platform.id} platform={platform} />
      ))}

      <OrbitControls
        enablePan={false}
        enableZoom={false}
        autoRotate
        autoRotateSpeed={0.25}
        minPolarAngle={Math.PI / 2.6}
        maxPolarAngle={Math.PI / 1.8}
      />
    </Canvas>
  );
}
