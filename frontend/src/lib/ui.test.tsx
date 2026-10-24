import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError } from "../api/client";
import { Field, ProvenanceBadge, StatusBadge } from "../components/ui";
import { ago, duration, fmtNum, titleCase } from "./format";

describe("format helpers", () => {
  it("formats numbers, durations and labels", () => {
    expect(fmtNum(null)).toBe("—");
    expect(fmtNum(1234.56)).toBe("1,235");
    expect(fmtNum(3.14159)).toBe("3.1");
    expect(duration(45)).toBe("45 s");
    expect(duration(7200)).toBe("2 h");
    expect(duration(600)).toBe("10 min");
    expect(titleCase("in_progress")).toBe("In Progress");
    expect(ago(null)).toBe("never");
  });
});

describe("provenance and status badges", () => {
  it("labels demo data distinctly and explains it", () => {
    render(<ProvenanceBadge p="demo" />);
    const badge = screen.getByText("Demo");
    expect(badge.closest(".badge")?.className).toContain("b-demo");
    expect(badge.closest(".badge")?.getAttribute("title")).toMatch(/SYNTHETIC/);
  });
  it("treats synthetic like demo", () => {
    render(<ProvenanceBadge p="synthetic" />);
    expect(screen.getByText("Synthetic").closest(".badge")?.className).toContain("b-demo");
  });
  it("renders failure states with the error style", () => {
    render(<StatusBadge s="timed_out" />);
    expect(screen.getByText("Timed Out").closest(".badge")?.className).toContain("b-error");
  });
});

describe("Field", () => {
  it("associates the label with its control and exposes errors", () => {
    render(<Field label="Title" error="Required"><input /></Field>);
    const input = screen.getByLabelText("Title");
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(document.getElementById(input.getAttribute("aria-describedby")!)?.textContent).toBe("Required");
  });
});

describe("api client", () => {
  afterEach(() => vi.restoreAllMocks());

  it("sends the CSRF token on mutations and parses structured errors", async () => {
    document.cookie = "swarmos_csrf=tok123";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "forbidden", message: "Nope", correlation_id: "abc" } }), { status: 403 }),
    );
    await expect(api("/x", { method: "POST", body: { a: 1 } })).rejects.toMatchObject({ status: 403, code: "forbidden", correlationId: "abc" });
    const headers = fetchMock.mock.calls[0][1]!.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("tok123");
  });

  it("maps network failures to a typed error", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    const err = (await api("/x").catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("network_error");
  });
});
