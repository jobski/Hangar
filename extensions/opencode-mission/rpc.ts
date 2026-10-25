// Portable JSON schemas keep the sidebar and server on the same RPC contract.
export const missionRPC = {
  id: "hangar.mission",
  methods: {
    action: {
      input: { type: "object", properties: { sessionID: { type: "string" }, action: { type: "string" }, payload: { type: "string" } }, required: ["sessionID", "action", "payload"], additionalProperties: false },
      output: { type: "object", properties: { json: { type: "string" } }, required: ["json"], additionalProperties: false },
      errors: { "mission.action": { type: "object", properties: {}, additionalProperties: false } },
    },
  },
  events: {},
} as const;
