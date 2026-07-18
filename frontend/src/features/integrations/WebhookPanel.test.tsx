import { afterEach, describe, expect, it, mock } from "bun:test";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { OrganizationDashboard } from "../organizations/OrganizationDashboard";

const originalFetch = globalThis.fetch;

function setCookie(value: string) {
  Reflect.set(document, "cookie", value);
}

function installFetchMock(
  implementation: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
) {
  const fetchMock = mock(implementation);
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: fetchMock,
    writable: true,
  });

  return fetchMock;
}

function organizationDashboardResponse() {
  return new Response(
    JSON.stringify({
      organization: { name: "Loja Horizonte", document_masked: "12.345.***/0001-**" },
      membership: { role: "OWNER" },
      balances: { held_brl_minor: 5000000, available_brl_minor: 245000 },
      upcoming_releases: [],
      api_keys: [],
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  cleanup();
  setCookie("escrow_csrf=; Max-Age=0; path=/");
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: originalFetch,
    writable: true,
  });
  mock.restore();
});

describe("painel de webhooks da organização", () => {
  it("lista entregas pendentes, entregues e falhadas e reenvia apenas as falhadas", async () => {
    const fetchMock = installFetchMock((input, init) => {
      const url = String(input);
      if (url === "/api/v1/organizations/current/") {
        return Promise.resolve(organizationDashboardResponse());
      }
      if (url === "/api/v1/organizations/current/api-keys/") {
        return Promise.resolve(jsonResponse({ api_keys: [] }));
      }
      if (url === "/api/v1/organizations/current/webhooks/") {
        return Promise.resolve(
          jsonResponse({
            webhook_endpoints: [
              {
                id: "endpoint_01",
                url: "https://hooks.example.test/escrow",
                is_active: true,
                previous_secret_expires_at: null,
                created_at: "2026-07-18T10:00:00Z",
              },
            ],
          }),
        );
      }
      if (url === "/api/v1/organizations/current/webhook-deliveries/" && init?.method !== "POST") {
        return Promise.resolve(
          jsonResponse({
            webhook_deliveries: [
              {
                id: "delivery_retry",
                endpoint_id: "endpoint_01",
                event_id: "event_01",
                agreement_id: "agr_01",
                event_type: "agreement.status_changed",
                sequence: 3,
                status: "RETRYING",
                attempts: 2,
                next_attempt_at: "2026-07-18T15:00:00Z",
                delivered_at: null,
                last_response_status: 500,
                last_error: "Http500",
                replay_count: 0,
              },
              {
                id: "delivery_failed",
                endpoint_id: "endpoint_01",
                event_id: "event_02",
                agreement_id: "agr_01",
                event_type: "agreement.status_changed",
                sequence: 4,
                status: "FAILED",
                attempts: 5,
                next_attempt_at: null,
                delivered_at: null,
                last_response_status: 404,
                last_error: "Http404",
                replay_count: 0,
              },
            ],
          }),
        );
      }
      if (url === "/api/v1/auth/csrf/") {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (url.endsWith("/webhook-deliveries/delivery_failed/replay/")) {
        return Promise.resolve(
          jsonResponse({
            webhook_delivery: {
              id: "delivery_failed",
              endpoint_id: "endpoint_01",
              event_id: "event_02",
              agreement_id: "agr_01",
              event_type: "agreement.status_changed",
              sequence: 4,
              status: "PENDING",
              attempts: 5,
              next_attempt_at: "2026-07-18T15:05:00Z",
              delivered_at: null,
              last_response_status: 404,
              last_error: "",
              replay_count: 1,
            },
          }),
        );
      }
      return Promise.reject(new Error(`URL inesperada: ${url}`));
    });

    render(<OrganizationDashboard onLogout={() => undefined} onReturnToLogin={() => undefined} />);

    await waitFor(() => {
      expect(screen.getByText("https://hooks.example.test/escrow")).toBeInTheDocument();
    });
    expect(screen.getByText("Reagendada")).toBeInTheDocument();
    expect(screen.getByText("Falhou")).toBeInTheDocument();
    expect(screen.getByText("Http500")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reenviar" }));

    await waitFor(() => {
      expect(screen.getByText("Pendente")).toBeInTheDocument();
    });

    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          String(url) ===
          "/api/v1/organizations/current/webhook-deliveries/delivery_failed/replay/",
      ),
    ).toBe(true);
  });

  it("configura um endpoint e revela o segredo de assinatura uma única vez", async () => {
    const fetchMock = installFetchMock((input, init) => {
      const url = String(input);
      if (url === "/api/v1/organizations/current/") {
        return Promise.resolve(organizationDashboardResponse());
      }
      if (url === "/api/v1/organizations/current/api-keys/") {
        return Promise.resolve(jsonResponse({ api_keys: [] }));
      }
      if (url === "/api/v1/organizations/current/webhook-deliveries/") {
        return Promise.resolve(jsonResponse({ webhook_deliveries: [] }));
      }
      if (url === "/api/v1/organizations/current/webhooks/" && init?.method !== "POST") {
        return Promise.resolve(jsonResponse({ webhook_endpoints: [] }));
      }
      if (url === "/api/v1/auth/csrf/") {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      if (url === "/api/v1/organizations/current/webhooks/" && init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              webhook_endpoint: {
                id: "endpoint_02",
                url: "https://hooks.example.test/escrow",
                is_active: true,
                previous_secret_expires_at: null,
                created_at: "2026-07-18T10:00:00Z",
              },
              secret: "whsec_segredo_unico",
            },
            201,
          ),
        );
      }
      return Promise.reject(new Error(`URL inesperada: ${url}`));
    });

    render(<OrganizationDashboard onLogout={() => undefined} onReturnToLogin={() => undefined} />);

    await waitFor(() => {
      expect(screen.getByText(/Nenhum endpoint configurado/)).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Configurar endpoint" }));
    fireEvent.change(screen.getByLabelText(/URL HTTPS pública/), {
      target: { value: "https://hooks.example.test/escrow" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Salvar endpoint" }));

    await waitFor(() => {
      expect(
        screen.getByRole("dialog", { name: "Guarde o segredo de assinatura" }),
      ).toBeInTheDocument();
    });

    expect(screen.getByText("whsec_segredo_unico")).toBeInTheDocument();

    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url) === "/api/v1/organizations/current/webhooks/" &&
        (options as RequestInit | undefined)?.method === "POST",
    );
    expect(createCall).toBeDefined();
    if (!createCall) {
      throw new Error("A criação do endpoint não foi enviada.");
    }
    expect(JSON.parse(String((createCall[1] as RequestInit).body))).toEqual({
      url: "https://hooks.example.test/escrow",
    });

    fireEvent.click(screen.getByRole("button", { name: "Entendi e fechei" }));

    expect(screen.queryByText("whsec_segredo_unico")).not.toBeInTheDocument();
    expect(screen.getByText("https://hooks.example.test/escrow")).toBeInTheDocument();
  });
});
