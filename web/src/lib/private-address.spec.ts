import { describe, expect, it } from "vitest";
import { isLocalOnlyAddress } from "./private-address";

describe("isLocalOnlyAddress", () => {
  it("is true for loopback, private, link-local and local-network addresses", () => {
    for (const url of [
      "http://127.0.0.1:5000/openai/v1",
      "http://localhost:5000/mcp",
      "http://[::1]:5000/mcp",
      "http://10.0.0.5/anthropic",
      "http://172.16.4.2/x",
      "http://172.31.255.1/x",
      "http://192.168.1.50:5000/openai/v1",
      "http://169.254.10.10/x",
      "http://[fd12:3456::1]/x",
      "http://[fe80::1]/x",
      "http://azizam:5000/openai/v1",
      "http://gateway.local/x",
      "http://gw.lan/x",
      "http://gw.home.arpa/x",
    ]) {
      expect(isLocalOnlyAddress(url), url).toBe(true);
    }
  });

  it("is false for public hosts and addresses, including 172.x outside the private range", () => {
    for (const url of [
      "https://gw.example/openai/v1",
      "https://azizam.dev/mcp",
      "http://8.8.8.8/x",
      "http://172.32.0.1/x",
      "http://172.15.0.1/x",
      "https://[2001:db8::1]/x",
    ]) {
      expect(isLocalOnlyAddress(url), url).toBe(false);
    }
  });

  it("is false for text that is not an absolute address", () => {
    expect(isLocalOnlyAddress("not a url")).toBe(false);
    expect(isLocalOnlyAddress("/openai/v1")).toBe(false);
  });
});
