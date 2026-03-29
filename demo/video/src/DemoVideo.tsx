import {
  AbsoluteFill,
  Sequence,
  Video,
  Img,
  staticFile,
  useCurrentFrame,
  interpolate,
  spring,
  useVideoConfig,
} from "remotion";
import React from "react";

// ── Color palette (solarpunk) ──────────────────────────────────────────────
const COLORS = {
  bg: "#0a0e17",
  text: "#c8d3e0",
  cyan: "#4fc3f7",
  gold: "#fbbf24",
  green: "#66bb6a",
  red: "#ef5350",
  muted: "#546e7a",
};

// ── Timeline (frames at 24fps) ─────────────────────────────────────────────
const FPS = 24;
const sec = (s: number) => s * FPS;

// Each section with scene clip mapping
const SECTIONS = [
  {
    name: "THE PROBLEM",
    scenes: [
      { file: "scene_01_v1_00001.mp4", duration: sec(5) },
      { file: "scene_02_v1_00001.mp4", duration: sec(5) },
      { file: "scene_03_v1_00001.mp4", duration: sec(5) },
      { file: "scene_04_v1_00001.mp4", duration: sec(5) },
    ],
    narration: [
      "Institutional investors pay $24,000 a year for a Bloomberg terminal.",
      "They have teams of analysts running multi-factor analysis.",
      "Retail investors get... stock tips on Reddit.",
      "What if an autonomous AI agent could deliver institutional-grade analysis for five cents?",
    ],
  },
  {
    name: "ARCHITECTURE",
    scenes: [
      { file: "scene_05_v1_00001.mp4", duration: sec(5) },
      { file: "scene_06_v1_00001.mp4", duration: sec(5) },
      { file: "screen_architecture.png", duration: sec(5), type: "image" },
      { file: "scene_08_v1_00001.mp4", duration: sec(5) },
      { file: "scene_09_v1_00001.mp4", duration: sec(5) },
      { file: "scene_10_v1_00001.mp4", duration: sec(5) },
    ],
    narration: [
      "This is the Sovereign Market Intelligence Agent.",
      "It uses a ReAct reasoning loop — Thought, Action, Observation — powered by Claude.",
      "Five real data sources: Yahoo Finance, FRED, STOCK Act, news feeds, Alpaca Markets.",
      "Congressional trading patterns, macroeconomic regime detection, technical analysis.",
      "All through an 8-layer safety system.",
      "Not suggestions to the AI — structural guardrails in code.",
    ],
  },
  {
    name: "LIVE DEMO",
    scenes: [
      { file: "scene_11_v1_00001.mp4", duration: sec(5) },
      { file: "screen_terminal_launch.png", duration: sec(5), type: "image" },
      { file: "screen_discover.png", duration: sec(5), type: "image" },
      { file: "scene_14_v1_00001.mp4", duration: sec(5) },
      { file: "screen_plan.png", duration: sec(5), type: "image" },
      { file: "screen_execute.png", duration: sec(5), type: "image" },
      { file: "scene_17_v1_00001.mp4", duration: sec(5) },
      { file: "screen_verify.png", duration: sec(5), type: "image" },
    ],
    narration: [
      "Let's watch it work.",
      "python main.py --autonomous",
      "Phase one: Discover. Scanning the market universe.",
      "",
      "Phase two: Plan. Chain-of-thought reasoning across technicals, sentiment, and macro.",
      "Phase three: Execute. Every order passes all eight safety layers.",
      "",
      "Phase four: Verify. Post-execution audit confirms all constraints hold.",
    ],
  },
  {
    name: "INTEGRATIONS",
    scenes: [
      { file: "scene_19_v1_00001.mp4", duration: sec(5) },
      { file: "screen_agent_log.png", duration: sec(5), type: "image" },
      { file: "scene_21_v1_00001.mp4", duration: sec(5) },
      { file: "screen_erc8004.png", duration: sec(5), type: "image" },
      { file: "scene_23_v1_00001.mp4", duration: sec(5) },
      { file: "scene_24_v1_00001.mp4", duration: sec(5) },
    ],
    narration: [
      "Every decision is captured in a structured log.",
      "Uploaded to IPFS via Storacha. Content-addressed. Immutable. Verifiable.",
      "",
      "ERC-8004 on Base L2 gives the agent a verifiable on-chain identity.",
      "",
      "Lit Protocol encrypts premium signals. Only token holders decrypt the alpha.",
    ],
  },
  {
    name: "SAFETY",
    scenes: [
      { file: "scene_25_v1_00001.mp4", duration: sec(5) },
      { file: "screen_safety_1_4.png", duration: sec(5), type: "image" },
      { file: "screen_safety_5_8.png", duration: sec(5), type: "image" },
      { file: "scene_28_v1_00001.mp4", duration: sec(5) },
      { file: "screen_guardrails.png", duration: sec(5), type: "image" },
      { file: "scene_30_v1_00001.mp4", duration: sec(5) },
    ],
    narration: [
      "Eight layers of structural safety.",
      "Position limits. Macro-aware sizing. Sector concentration. Circuit breakers.",
      "Cash reserves. VIX-adaptive stops. Anomaly detection. Market hours.",
      "The agent can't reason its way around these.",
      "They're code, not prompts. In a CRITICAL regime, the modifier goes to zero.",
      "",
    ],
  },
  {
    name: "IMPACT",
    scenes: [
      { file: "scene_31_v1_00001.mp4", duration: sec(5) },
      { file: "scene_32_v1_00001.mp4", duration: sec(5) },
      { file: "scene_33_v1_00001.mp4", duration: sec(5) },
      { file: "scene_34_v1_00001.mp4", duration: sec(5) },
      { file: "scene_35_v1_00001.mp4", duration: sec(5) },
      { file: "scene_36_v1_00001.mp4", duration: sec(5) },
    ],
    narration: [
      "Bloomberg: $24,000 a year.",
      "Sovereign Agent: five cents per analysis.",
      "Every analysis verifiable on-chain.",
      "Token-gated intelligence. Democratized access.",
      "Autonomous systems building autonomous systems. It's agents all the way down.",
      "",
    ],
  },
];

// ── Components ─────────────────────────────────────────────────────────────

const FadeIn: React.FC<{ children: React.ReactNode; delay?: number }> = ({
  children,
  delay = 0,
}) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame - delay, [0, 12], [0, 1], {
    extrapolateRight: "clamp",
  });
  return <div style={{ opacity }}>{children}</div>;
};

const SubtitleBar: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const opacity = interpolate(frame, [0, 8, 96, 110], [0, 1, 1, 0], {
    extrapolateRight: "clamp",
  });

  if (!text) return null;

  return (
    <div
      style={{
        position: "absolute",
        bottom: 60,
        left: 0,
        right: 0,
        textAlign: "center",
        opacity,
        zIndex: 10,
      }}
    >
      <span
        style={{
          background: "rgba(10, 14, 23, 0.85)",
          color: COLORS.text,
          padding: "12px 28px",
          borderRadius: 6,
          fontSize: 22,
          fontFamily: "'SF Mono', 'Fira Code', monospace",
          lineHeight: 1.5,
          maxWidth: "80%",
          display: "inline-block",
        }}
      >
        {text}
      </span>
    </div>
  );
};

const SectionTitle: React.FC<{ name: string }> = ({ name }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const scale = spring({ frame, fps, config: { damping: 12 } });
  const opacity = interpolate(frame, [0, 6, 18, 24], [0, 1, 1, 0], {
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        background: COLORS.bg,
        opacity,
      }}
    >
      <div
        style={{
          fontSize: 36,
          fontFamily: "'SF Mono', monospace",
          color: COLORS.cyan,
          letterSpacing: 8,
          fontWeight: 600,
          transform: `scale(${scale})`,
        }}
      >
        {name}
      </div>
    </AbsoluteFill>
  );
};

const ClosingTitle: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const titleOpacity = interpolate(frame, [0, 24, 72, 96], [0, 1, 1, 0.8], {
    extrapolateRight: "clamp",
  });
  const subtitleOpacity = interpolate(frame, [36, 60], [0, 1], {
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        justifyContent: "center",
        alignItems: "center",
        background: COLORS.bg,
      }}
    >
      <div style={{ textAlign: "center" }}>
        <div
          style={{
            fontSize: 42,
            fontFamily: "'SF Mono', monospace",
            color: COLORS.cyan,
            fontWeight: 600,
            opacity: titleOpacity,
            marginBottom: 20,
          }}
        >
          SOVEREIGN MARKET INTELLIGENCE AGENT
        </div>
        <div
          style={{
            fontSize: 24,
            fontFamily: "'SF Mono', monospace",
            color: COLORS.gold,
            opacity: subtitleOpacity,
            fontStyle: "italic",
          }}
        >
          Let the agent cook.
        </div>
      </div>
    </AbsoluteFill>
  );
};

// ── Main Composition ───────────────────────────────────────────────────────

export const DemoVideo: React.FC = () => {
  let frameOffset = 0;

  const sequences: React.ReactNode[] = [];

  for (const section of SECTIONS) {
    // Section title card (1 second)
    sequences.push(
      <Sequence from={frameOffset} durationInFrames={sec(1)} key={`title-${section.name}`}>
        <SectionTitle name={section.name} />
      </Sequence>
    );
    frameOffset += sec(1);

    // Scene clips with subtitles
    for (let i = 0; i < section.scenes.length; i++) {
      const scene = section.scenes[i];
      const narration = section.narration[i] || "";

      sequences.push(
        <Sequence from={frameOffset} durationInFrames={scene.duration} key={`scene-${frameOffset}`}>
          <AbsoluteFill style={{ background: COLORS.bg }}>
            {scene.type === "image" ? (
              <Img
                src={staticFile(`clips/${scene.file}`)}
                style={{ width: "100%", height: "100%", objectFit: "contain" }}
              />
            ) : (
              <Video
                src={staticFile(`clips/${scene.file}`)}
                style={{ width: "100%", height: "100%", objectFit: "cover" }}
              />
            )}
            <SubtitleBar text={narration} />
          </AbsoluteFill>
        </Sequence>
      );
      frameOffset += scene.duration;
    }
  }

  // Closing title (5 seconds)
  sequences.push(
    <Sequence from={frameOffset} durationInFrames={sec(5)} key="closing">
      <ClosingTitle />
    </Sequence>
  );

  return <AbsoluteFill style={{ background: COLORS.bg }}>{sequences}</AbsoluteFill>;
};
