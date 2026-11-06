import { describe, expect, it } from "vitest";
import { addressReach, isLocalOnlyAddress } from "./private-address";

describe("addressReach", () => {
  it("is loopback for 127.0.0.0/8, ::1 and localhost, and only those", () => {
    for (const url of [
      "http://127.0.0.1:5000/openai/v1",
      "http://127.8.9.10/x",
      "http://localhost:5000/mcp",
      "http://app.localhost:5000/x",
      "http://[::1]:5000/mcp",
    ]) {
      expect(addressReach(url), url).toBe("loopback");
    }
  });

  it("is lan for private, link-local and local-network addresses and names", () => {
    for (const url of [
      "http://192.168.1.50:5000/openai/v1",
      "http://10.0.0.5/anthropic",
      "http://172.20.0.1/x",
      "http://169.254.10.10/x",
      "http://[fd12:3456::1]/x",
      "http://azizam:5000/openai/v1",
      "http://gateway.local/x",
    ]) {
      expect(addressReach(url), url).toBe("lan");
    }
  });

  it("reads an IPv4-mapped IPv6 address by the IPv4 address it carries", () => {
    expect(addressReach("http://[::ffff:127.0.0.1]:5000/x")).toBe("loopback");
    expect(addressReach("http://[::ffff:192.168.1.5]:5000/x")).toBe("lan");
    expect(addressReach("http://[::ffff:10.1.2.3]/x")).toBe("lan");
    expect(addressReach("http://[::ffff:8.8.8.8]/x")).toBe("public");
  });

  it("is public for anything else, and for text that is not an absolute address", () => {
    expect(addressReach("https://gw.example/openai/v1")).toBe("public");
    expect(addressReach("http://8.8.8.8/x")).toBe("public");
    expect(addressReach("not a url")).toBe("public");
  });
});

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
