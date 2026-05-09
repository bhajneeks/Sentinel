"use client";

import { Billboard, Float, Html, Line, OrbitControls, RoundedBox, Stars } from "@react-three/drei";
import { Canvas, useFrame } from "@react-three/fiber";
import { useQuery } from "convex/react";
import { useMemo, useRef } from "react";
import * as THREE from "three";
import { api } from "../../../../convex/_generated/api";

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
    position: [0, -1.6, 1.8],
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
  {
    id: "tiktok",
    label: "TikTok",
    url: "tiktok.com",
    initial: "♪",
    color: "#ff0050",
    emissive: "#00f2ea",
    position: [0, 1.9, -1.5],
    phase: 5.1,
  },
];

const FALLBACK_PLATFORM: Omit<Platform, "id" | "label" | "url" | "initial" | "position" | "phase"> = {
  color: "#a78bfa",
  emissive: "#c4b5fd",
};

const MAX_EXTRA_LIVE_NODES = 3;

type LiveSession = {
  _id: string;
  platform: string;
  query: string;
  liveUrl: string;
  startedAt?: number;
  participant?: string | null;
  cloudSessionId?: string | null;
  energy?: number | null;
  restartCount?: number;
  lastDiagnosis?: string | null;
};

type AgentSceneProps = {
  participant: string | null;
};

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

type LineHandle = {
  geometry: {
    setPositions(positions: number[]): void;
    computeLineDistances?(): void;
  };
  material: { dashOffset?: number };
};

const FLOAT_X_AMP = 0.06;
const FLOAT_Y_AMP = 0.08;

function _energyColor(energy: number): string {
  return energy > 60 ? "#34d399" : energy > 25 ? "#fbbf24" : "#fb7185";
}

/**
 * Energy bar — rendered as an HTML overlay floating ABOVE every BrowserChrome,
 * not inside the chrome interior. The live-stream iframe occupies the entire
 * screen-panel area and renders in CSS3D, which visually covers anything
 * positioned beneath it. Putting the bar above the chrome's title bar means
 * it's always visible regardless of iframe size, camera angle, or occlusion.
 *
 * States:
 *   - active session w/ energy   → colored fill + numeric percentage
 *   - active session, energy null → amber 50% + "warming up"
 *   - no session (idle slot)      → muted track only + "idle"
 */
function EnergyBarOverlay({
  energy,
  restartCount,
  isLive,
}: {
  energy: number | null;
  restartCount: number;
  isLive: boolean;
}) {
  let ratio: number;
  let color: string;
  let label: string;

  if (!isLive) {
    ratio = 0;
    color = "#6b7280";
    label = "idle";
  } else if (energy === null) {
    ratio = 0.5;
    color = "#fbbf24";
    label = "warming up";
  } else {
    const clamped = Math.max(0, Math.min(100, energy));
    ratio = clamped / 100;
    color = _energyColor(clamped);
    label = `⚡ ${Math.round(clamped)}`;
  }

  return (
    <Html
      center
      distanceFactor={3.2}
      position={[0, 0.86, 0.05]}
      occlude={false}
      zIndexRange={[100000, 100000]}
    >
      <div
        className="pointer-events-none flex w-[180px] flex-col items-stretch gap-1"
        style={{ filter: isLive ? `drop-shadow(0 0 8px ${color}66)` : undefined }}
      >
        <div className="flex items-center justify-between font-mono text-[10px] font-semibold tracking-[0.2em] uppercase">
          <span style={{ color }}>{label}</span>
          {restartCount > 0 ? (
            <span className="rounded-full bg-violet-500/30 px-1.5 py-[1px] text-violet-100 text-[8px] ring-1 ring-violet-400/40">
              ×{restartCount}
            </span>
          ) : null}
        </div>
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-black/60 ring-1 ring-white/10">
          <div
            className="h-full transition-[width] duration-500"
            style={{ width: `${ratio * 100}%`, background: color }}
          />
        </div>
      </div>
    </Html>
  );
}

function PlatformAssembly({
  platform,
  live,
}: {
  platform: Platform;
  live?: LiveSession | null;
}) {
  const browserGroupRef = useRef<THREE.Group>(null);
  const lineRef = useRef<LineHandle | null>(null);

  useFrame((state) => {
    const t = state.clock.elapsedTime;
    const dx = Math.cos(t * 0.5 + platform.phase) * FLOAT_X_AMP;
    const dy = Math.sin(t * 0.7 + platform.phase) * FLOAT_Y_AMP;

    if (browserGroupRef.current) {
      browserGroupRef.current.position.set(dx, dy, 0);
    }

    const line = lineRef.current;
    if (line) {
      line.geometry.setPositions([
        0,
        0,
        0,
        platform.position[0] + dx,
        platform.position[1] + dy,
        platform.position[2],
      ]);
      line.geometry.computeLineDistances?.();
      if (line.material.dashOffset !== undefined) {
        line.material.dashOffset = -t * 0.6 + platform.phase;
      }
    }
  });

  return (
    <>
      <Line
        ref={(el) => {
          lineRef.current = el as unknown as LineHandle | null;
        }}
        points={[
          [0, 0, 0],
          platform.position,
        ]}
        color={platform.color}
        lineWidth={live ? 2.2 : 1.5}
        transparent
        opacity={live ? 0.85 : 0.6}
        dashed
        dashSize={0.18}
        gapSize={0.12}
      />
      <Billboard
        follow
        lockX={false}
        lockY={false}
        lockZ={false}
        position={platform.position}
      >
        <group ref={browserGroupRef}>
          <BrowserChrome platform={platform} live={live ?? null} />
        </group>
      </Billboard>
    </>
  );
}

function BrowserChrome({
  platform,
  live,
}: {
  platform: Platform;
  live: LiveSession | null;
}) {
  const screenRef = useRef<THREE.Mesh>(null);

  useFrame((state) => {
    if (!screenRef.current) return;
    const mat = screenRef.current.material as THREE.MeshStandardMaterial;
    mat.emissiveIntensity =
      0.35 + Math.sin(state.clock.elapsedTime * 1.2 + platform.phase) * 0.08;
  });

  const addressText = live ? "live.browser-use.com" : platform.url;

  return (
    <group>
      {/* glow halo behind */}
      <mesh position={[0, 0, -0.04]}>
        <planeGeometry args={[2.2, 1.55]} />
        <meshBasicMaterial color={platform.emissive} transparent opacity={live ? 0.22 : 0.14} />
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
        <div className="pointer-events-none flex items-center gap-1 whitespace-nowrap font-mono text-[8px] text-zinc-400 tracking-wider">
          {live ? (
            <span className="flex items-center gap-1 rounded-full bg-rose-500/30 px-1.5 py-[1px] font-semibold text-[7px] text-rose-200 ring-1 ring-rose-400/40">
              <span className="h-1 w-1 animate-pulse rounded-full bg-rose-300" />
              LIVE
            </span>
          ) : null}
          {addressText}
        </div>
      </Html>

      {/* screen content panel — rendered only when not live (live iframe replaces it) */}
      {!live && (
        <>
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
          <ContentRows color={platform.emissive} />
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
        </>
      )}

      {live && <LiveScreen liveUrl={live.liveUrl} platform={platform} query={live.query} />}
      <EnergyBarOverlay
        isLive={!!live}
        energy={live && typeof live.energy === "number" ? live.energy : null}
        restartCount={live?.restartCount ?? 0}
      />
    </group>
  );
}

function LiveScreen({
  liveUrl,
  platform,
  query,
}: {
  liveUrl: string;
  platform: Platform;
  query: string;
}) {
  // drei's <Html transform> uses a CSS3D matrix with scale 1/(distanceFactor||10/400).
  // distanceFactor=1 + 372×200 px rendered at ~25% of the chrome plane
  // (see fix history). 4× pixel dims at the same distanceFactor fills it.
  return (
    <Html
      transform
      occlude
      position={[0, -0.085, 0.035]}
      distanceFactor={1}
      style={{ pointerEvents: "auto" }}
    >
      <div
        className="relative overflow-hidden rounded-[6px] ring-1 ring-white/10"
        style={{
          width: 1488,
          height: 800,
          boxShadow: `0 0 36px ${platform.emissive}55`,
        }}
      >
        <iframe
          src={liveUrl}
          loading="lazy"
          referrerPolicy="no-referrer"
          sandbox="allow-scripts allow-same-origin"
          allow="autoplay; clipboard-read; clipboard-write"
          title={`${platform.label} live · ${query}`}
          className="h-full w-full border-0"
        />
        <div className="pointer-events-none absolute right-3 bottom-3 rounded-full bg-black/70 px-3 py-1 font-mono text-[14px] text-zinc-200 ring-1 ring-white/10 backdrop-blur">
          {platform.label} · {query}
        </div>
      </div>
    </Html>
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

/** Compute a deterministic ring slot for an extra orbital node. */
function orbitalPosition(index: number, total: number): [number, number, number] {
  const denom = Math.max(total, 3);
  const theta = (Math.PI * 2 * index) / denom;
  const radius = 4.2;
  const y = 1.4 + (index % 2 === 0 ? 0 : -0.6);
  return [Math.cos(theta) * radius, y, Math.sin(theta) * radius];
}

function makeExtraPlatform(session: LiveSession, index: number, total: number): Platform {
  const base = PLATFORMS.find((p) => p.id === session.platform);
  return {
    id: `extra:${session._id}`,
    label: base?.label ?? session.platform,
    url: base?.url ?? "browser-use.com",
    initial: base?.initial ?? "·",
    color: base?.color ?? FALLBACK_PLATFORM.color,
    emissive: base?.emissive ?? FALLBACK_PLATFORM.emissive,
    position: orbitalPosition(index, total),
    phase: (index * 1.7) % (Math.PI * 2),
  };
}

export default function AgentScene({ participant }: AgentSceneProps) {
  const cloudSessions =
    useQuery(
      api.sessions.activeCloud,
      participant ? { participant } : { participant: undefined },
    ) ?? [];

  const { slotByPlatform, extras } = useMemo(() => {
    const grouped = new Map<string, LiveSession[]>();
    for (const s of cloudSessions) {
      const list = grouped.get(s.platform) ?? [];
      list.push(s);
      grouped.set(s.platform, list);
    }
    const slot = new Map<string, LiveSession>();
    const overflow: LiveSession[] = [];
    for (const platform of PLATFORMS) {
      const list = grouped.get(platform.id) ?? [];
      if (list.length > 0) {
        slot.set(platform.id, list[0]);
        overflow.push(...list.slice(1));
      }
    }
    for (const [pid, list] of grouped) {
      if (!PLATFORMS.find((p) => p.id === pid)) {
        overflow.push(...list);
      }
    }
    return {
      slotByPlatform: slot,
      extras: overflow.slice(0, MAX_EXTRA_LIVE_NODES),
    };
  }, [cloudSessions]);

  return (
    <Canvas
      camera={{ position: [0, 1.5, 7], fov: 50 }}
      gl={{
        antialias: true,
        alpha: true,
        powerPreference: "high-performance",
        failIfMajorPerformanceCaveat: false,
      }}
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
        <PlatformAssembly
          key={platform.id}
          platform={platform}
          live={slotByPlatform.get(platform.id) ?? null}
        />
      ))}

      {extras.map((session, i) => (
        <PlatformAssembly
          key={session._id}
          platform={makeExtraPlatform(session, i, extras.length)}
          live={session}
        />
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

