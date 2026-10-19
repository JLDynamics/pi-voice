// The transcript renderer for saved and live voice entries.
//
// Split out of `voice.ts` so that file stays within its line budget; this is the
// only place that knows how a `pi-voice-face` entry becomes text on screen.

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type { Component } from "@earendil-works/pi-tui";
import { AGENT_LABEL, USER_LABEL } from "./voice-core.ts";
import { faceLines, sessionFaceLines, spokenFaceLines } from "./voice-face.ts";

/** Custom entry type holding one saved or live voice line. */
export const FACE_TYPE = "pi-voice-face";

/** What the renderer needs from the Voice instance: the live text maps and state. */
export type FaceHost = {
  readonly spokenTexts: Map<string, string>;
  readonly heardTexts: Map<string, string>;
  readonly voiceActive: () => boolean;
};

/**
 * Register the renderer. Revisions of one utterance share an item id, so the
 * live variants read their text from the host's map and re-render in place
 * instead of appending a line per revision.
 */
export function registerFaceRenderer(pi: ExtensionAPI, host: FaceHost): void {
  if (typeof pi.registerEntryRenderer !== "function") return;
  pi.registerEntryRenderer<{ kind: string; text?: string; id?: string }>(FACE_TYPE, (entry) => {
    const data = entry.data;
    if (data?.kind === "spoken-final" && data.id && data.text != null) {
      host.spokenTexts.set(data.id, data.text);
      return;
    }
    if (data?.kind === "heard-final" && data.id && data.text != null) {
      host.heardTexts.set(data.id, data.text);
      return;
    }
    if (data?.kind === "heard-live" && data.id) {
      if (!host.heardTexts.has(data.id)) host.heardTexts.set(data.id, data.text ?? "");
      const id = data.id;
      return {
        render: (width: number) => faceLines(USER_LABEL, host.heardTexts.get(id) ?? "", width),
        invalidate() {},
      };
    }
    if (data?.kind === "spoken-live" && data.id) {
      if (!host.spokenTexts.has(data.id)) host.spokenTexts.set(data.id, data.text ?? "");
      const id = data.id;
      return {
        render: (width: number) => spokenFaceLines(host.spokenTexts.get(id) ?? "", width),
        invalidate() {},
      };
    }
    if (!data?.text) return;
    const who = data.kind === "heard" ? USER_LABEL : data.kind === "spoken" ? AGENT_LABEL : "pi";
    const text = data.text;
    const face: Component = {
      render: (width: number) => sessionFaceLines(who, text, width, host.voiceActive()),
      invalidate() {},
    };
    return face;
  });
}
