"use client";

import { Float, Html, OrbitControls, Stars } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { useMemo, useRef } from "react";
import * as THREE from "three";

type Platform = {
  id: string;
  label: string;
  initial: string;
  color: string;
  emissive: string;
  orbitRadius: number;
  orbitSpeed: number;
  orbitTilt: number;
  phase: number;
};

const PLATFORMS: Platform[] = [
  {
    id: "reddit",
    label: "Reddit",
    initial: "R",
    color: "#ff4500",
    emissive: "#ff7849",
    orbitRadius: 3.0,
    orbitSpeed: 0.18,
    orbitTilt: 0.25,
    phase: 0,
  },
  {
    id: "x",
    label: "X",
    initial: "𝕏",
    color: "#e2e8f0",
    emissive: "#94a3b8",
    orbitRadius: 4.2,
    orbitSpeed: 0.12,
    orbitTilt: -0.45,
    phase: 2.1,
  },
  {
    id: "linkedin",
    label: "LinkedIn",
    initial: "in",
    color: "#0a66c2",
    emissive: "#3b82f6",
    orbitRadius: 3.6,
    orbitSpeed: 0.22,
    orbitTilt: 0.55,
    phase: 4.3,
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
    <Float speed={1.2} rotationIntensity={0} floatIntensity={0.3}>
      <group>
        <mesh ref={meshRef}>
          <icosahedronGeometry args={[1.1, 1]} />
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
          <icosahedronGeometry args={[0.7, 0]} />
          <meshStandardMaterial
            color="#a78bfa"
            emissive="#c4b5fd"
            emissiveIntensity={1.2}
            metalness={0.9}
            roughness={0.1}
          />
        </mesh>
        <pointLight color="#a78bfa" intensity={2} distance={8} />
        <Html center distanceFactor={10} position={[0, -1.7, 0]}>
          <div className="pointer-events-none whitespace-nowrap text-center font-mono text-[10px] text-violet-200/80 tracking-widest uppercase">
            Spectrum Core
          </div>
        </Html>
      </group>
    </Float>
  );
}

function PlatformNode({ platform }: { platform: Platform }) {
  const groupRef = useRef<THREE.Group>(null);
  const lineRef = useRef<THREE.Mesh>(null);
  const positionRef = useRef(new THREE.Vector3());

  useFrame((state) => {
    const t = state.clock.elapsedTime * platform.orbitSpeed + platform.phase;
    const x = Math.cos(t) * platform.orbitRadius;
    const z = Math.sin(t) * platform.orbitRadius;
    const y = Math.sin(t * 0.7) * platform.orbitRadius * platform.orbitTilt;
    positionRef.current.set(x, y, z);
    if (groupRef.current) {
      groupRef.current.position.copy(positionRef.current);
    }
    if (lineRef.current) {
      const length = positionRef.current.length();
      lineRef.current.scale.set(1, length, 1);
      lineRef.current.position.set(x / 2, y / 2, z / 2);
      lineRef.current.lookAt(positionRef.current);
      lineRef.current.rotateX(Math.PI / 2);
    }
  });

  return (
    <>
      <mesh ref={lineRef}>
        <cylinderGeometry args={[0.008, 0.008, 1, 6]} />
        <meshBasicMaterial color={platform.color} transparent opacity={0.25} />
      </mesh>
      <group ref={groupRef}>
        <mesh>
          <sphereGeometry args={[0.32, 32, 32]} />
          <meshStandardMaterial
            color={platform.color}
            emissive={platform.emissive}
            emissiveIntensity={0.7}
            metalness={0.4}
            roughness={0.3}
          />
        </mesh>
        <mesh>
          <sphereGeometry args={[0.42, 32, 32]} />
          <meshBasicMaterial
            color={platform.emissive}
            transparent
            opacity={0.12}
          />
        </mesh>
        <Html center distanceFactor={8} position={[0, 0.6, 0]}>
          <div className="pointer-events-none whitespace-nowrap rounded-full bg-black/40 px-2 py-0.5 text-[10px] text-white/90 backdrop-blur-md ring-1 ring-white/10">
            <span className="mr-1 font-bold" style={{ color: platform.color }}>
              {platform.initial}
            </span>
            {platform.label}
          </div>
        </Html>
      </group>
    </>
  );
}

function OrbitRings() {
  const ringRefs = useRef<(THREE.Mesh | null)[]>([]);

  useFrame((_, delta) => {
    for (const [i, ring] of ringRefs.current.entries()) {
      if (ring) {
        ring.rotation.z += delta * 0.05 * (i % 2 === 0 ? 1 : -1);
      }
    }
  });

  const rings = useMemo(
    () => PLATFORMS.map((p) => ({ radius: p.orbitRadius, tilt: p.orbitTilt })),
    []
  );

  return (
    <>
      {rings.map((r, i) => (
        <mesh
          key={`ring-${r.radius.toFixed(2)}-${r.tilt.toFixed(2)}`}
          ref={(el) => {
            ringRefs.current[i] = el;
          }}
          rotation={[Math.PI / 2 + r.tilt, 0, 0]}
        >
          <torusGeometry args={[r.radius, 0.005, 8, 128]} />
          <meshBasicMaterial color="#8b5cf6" transparent opacity={0.12} />
        </mesh>
      ))}
    </>
  );
}

export default function AgentScene() {
  return (
    <Canvas
      camera={{ position: [0, 2.5, 8], fov: 50 }}
      gl={{ antialias: true, alpha: true }}
      style={{ background: "transparent" }}
    >
      <color attach="background" args={["#05030f"]} />
      <fog attach="fog" args={["#05030f", 8, 18]} />

      <ambientLight intensity={0.15} />
      <directionalLight position={[5, 5, 5]} intensity={0.4} />
      <pointLight position={[-5, -3, -5]} color="#06b6d4" intensity={0.8} />

      <Stars
        radius={50}
        depth={30}
        count={1500}
        factor={3}
        fade
        speed={0.5}
      />

      <CoreNode />
      <OrbitRings />
      {PLATFORMS.map((platform) => (
        <PlatformNode key={platform.id} platform={platform} />
      ))}

      <OrbitControls
        enablePan={false}
        enableZoom={false}
        autoRotate
        autoRotateSpeed={0.4}
        minPolarAngle={Math.PI / 3}
        maxPolarAngle={(Math.PI * 2) / 3}
      />
    </Canvas>
  );
}
