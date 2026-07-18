import { afterEach, describe, expect, it, mock } from "bun:test";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";

const originalFetch = globalThis.fetch;

function installFetchMock(implementation: () => Promise<Response>) {
  const fetchMock = mock(implementation);
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: fetchMock,
    writable: true,
  });

  return fetchMock;
}

afterEach(() => {
  cleanup();
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: originalFetch,
    writable: true,
  });
  mock.restore();
});

describe("application shell", () => {
  it("shows a healthy custody rail after the readiness check succeeds", async () => {
    installFetchMock(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "ready" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    render(<App />);

    expect(screen.getByRole("status")).toHaveTextContent("Verificando operação");

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Operação normal");
    });

    expect(screen.getByText("Trilha de custódia")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Verificar novamente" })).toBeEnabled();
  });

  it("exposes a recovery action when the readiness check is unavailable", async () => {
    const fetchMock = installFetchMock(() => Promise.reject(new Error("offline")));

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Atenção operacional");
    });

    const retry = screen.getByRole("button", { name: "Verificar novamente" });
    fireEvent.click(retry);

    await waitFor(() => {
      expect(fetchMock.mock.calls).toHaveLength(2);
      expect(screen.getByRole("status")).toHaveTextContent("Atenção operacional");
    });
  });
});
