import { Composition } from "remotion";
import { DemoVideo } from "./DemoVideo";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition
        id="SovereignAgentDemo"
        component={DemoVideo}
        durationInFrames={24 * 180} // 3 minutes at 24fps
        fps={24}
        width={1280}
        height={720}
      />
    </>
  );
};
