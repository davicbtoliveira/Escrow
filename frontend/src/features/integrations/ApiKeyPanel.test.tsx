import { afterEach, describe, expect, it, mock } from "bun:test";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { organizationApi } from "../../lib/api";
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
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
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

describe("painel de chaves da organização", () => {
  it("envia corpo JSON também para a rotação padrão e preserva sobreposição zero", async () => {
    setCookie("escrow_csrf=teste-csrf; path=/");
    const fetchMock = installFetchMock(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            api_key: {
              id: "key_02",
              name: "Chave substituta",
              prefix: "esk_live_next",
              scopes: ["agreements:read"],
              expires_at: null,
              last_used_at: null,
              last_used_ip: null,
              status: "ACTIVE",
            },
            secret: "esk_live_next_novo_segredo",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    await organizationApi.rotateApiKey("key_01");
    await organizationApi.rotateApiKey("key_02", 0);

    expect(JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))).toEqual({});
    expect(JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body))).toEqual({
      overlap_seconds: 0,
    });
  });

  it("lista os metadados auditáveis de uma chave sem mostrar seu segredo", async () => {
    installFetchMock((input) => {
      if (String(input) === "/api/v1/organizations/current/") {
        return Promise.resolve(organizationDashboardResponse());
      }

      return Promise.resolve(
        new Response(
          JSON.stringify({
            api_keys: [
              {
                id: "key_01",
                name: "Produção marketplace",
                prefix: "esk_live_ab12",
                scopes: ["agreements:write", "agreements:read"],
                expires_at: "2026-08-01T00:00:00Z",
                last_used_at: "2026-07-18T14:30:00Z",
                last_used_ip: "203.0.113.14",
                status: "ACTIVE",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });

    render(<OrganizationDashboard onLogout={() => undefined} onReturnToLogin={() => undefined} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Chaves de integração" })).toBeInTheDocument();
    });

    expect(screen.getByText("esk_live_ab12")).toBeInTheDocument();
    expect(screen.getByText("Produção marketplace")).toBeInTheDocument();
    expect(screen.getByText("agreements:write")).toBeInTheDocument();
    expect(screen.getByText("203.0.113.14")).toBeInTheDocument();
    expect(screen.queryByText(/esk_live_ab12_[A-Za-z0-9]/)).not.toBeInTheDocument();
  });

  it("cria uma chave com escopos explícitos e revela o segredo apenas até o aviso ser fechado", async () => {
    const fetchMock = installFetchMock((input, init) => {
      if (String(input) === "/api/v1/organizations/current/") {
        return Promise.resolve(organizationDashboardResponse());
      }

      if (String(input) === "/api/v1/organizations/current/api-keys/" && init?.method !== "POST") {
        return Promise.resolve(
          new Response(JSON.stringify({ api_keys: [] }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      if (String(input) === "/api/v1/auth/csrf/") {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      return Promise.resolve(
        new Response(
          JSON.stringify({
            api_key: {
              id: "key_02",
              name: "Marketplace produção",
              prefix: "esk_live_f9a1",
              scopes: ["agreements:write", "agreements:read"],
              expires_at: "2026-08-01T23:59:59Z",
              last_used_at: null,
              last_used_ip: null,
              status: "ACTIVE",
            },
            secret: "esk_live_f9a1_super_segredo",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      );
    });

    render(<OrganizationDashboard onLogout={() => undefined} onReturnToLogin={() => undefined} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Criar chave de integração" })).toBeEnabled();
    });

    fireEvent.click(screen.getByRole("button", { name: "Criar chave de integração" }));
    fireEvent.change(screen.getByLabelText("Nome da chave"), {
      target: { value: "Marketplace produção" },
    });
    fireEvent.click(screen.getByRole("checkbox", { name: /Criar acordos/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Ler acordos/ }));
    fireEvent.change(screen.getByLabelText(/Expira em/), { target: { value: "2026-08-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Gerar chave" }));

    await waitFor(() => {
      expect(screen.getByRole("dialog", { name: "Guarde sua nova chave" })).toBeInTheDocument();
    });

    expect(screen.getByText("esk_live_f9a1_super_segredo")).toBeInTheDocument();
    expect(screen.getByText(/não será exibido novamente/i)).toBeInTheDocument();

    const createCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        url === "/api/v1/organizations/current/api-keys/" &&
        (options as RequestInit | undefined)?.method === "POST",
    );
    expect(createCall).toBeDefined();
    if (!createCall) {
      throw new Error("A criação da chave não foi enviada.");
    }
    const createOptions = createCall[1] as RequestInit;
    expect(JSON.parse(String(createOptions.body))).toEqual({
      name: "Marketplace produção",
      scopes: ["agreements:write", "agreements:read"],
      expires_at: "2026-08-01T23:59:59Z",
    });
    expect(new Headers(createOptions.headers).get("X-CSRFToken")).toBe("teste-csrf");

    fireEvent.click(screen.getByRole("button", { name: "Entendi e fechei" }));

    expect(screen.queryByText("esk_live_f9a1_super_segredo")).not.toBeInTheDocument();
  });

  it("rotaciona uma chave com sobreposição temporária e mostra apenas a substituta", async () => {
    const fetchMock = installFetchMock((input) => {
      if (String(input) === "/api/v1/organizations/current/") {
        return Promise.resolve(organizationDashboardResponse());
      }

      if (String(input) === "/api/v1/organizations/current/api-keys/") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              api_keys: [
                {
                  id: "key_01",
                  name: "Produção marketplace",
                  prefix: "esk_live_ab12",
                  scopes: ["agreements:write"],
                  expires_at: null,
                  last_used_at: null,
                  last_used_ip: null,
                  status: "ACTIVE",
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }

      if (String(input) === "/api/v1/auth/csrf/") {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      return Promise.resolve(
        new Response(
          JSON.stringify({
            api_key: {
              id: "key_02",
              name: "Produção marketplace — rotação",
              prefix: "esk_live_next",
              scopes: ["agreements:write"],
              expires_at: null,
              last_used_at: null,
              last_used_ip: null,
              status: "ACTIVE",
            },
            previous_api_key: {
              id: "key_01",
              name: "Produção marketplace",
              prefix: "esk_live_ab12",
              scopes: ["agreements:write"],
              expires_at: "2030-08-01T23:59:59Z",
              last_used_at: null,
              last_used_ip: null,
              status: "ACTIVE",
            },
            secret: "esk_live_next_novo_segredo",
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        ),
      );
    });

    render(<OrganizationDashboard onLogout={() => undefined} onReturnToLogin={() => undefined} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Rotacionar Produção marketplace" })).toBeEnabled();
    });

    fireEvent.click(screen.getByRole("button", { name: "Rotacionar Produção marketplace" }));
    expect(screen.getByRole("dialog", { name: "Rotacionar chave" })).toHaveTextContent("24 horas");
    fireEvent.click(screen.getByRole("button", { name: "Confirmar rotação" }));

    await waitFor(() => {
      expect(screen.getByText("esk_live_next_novo_segredo")).toBeInTheDocument();
    });
    expect(screen.getByText("esk_live_ab12").closest("tr")).toHaveTextContent("2030");

    const rotationCall = fetchMock.mock.calls.find(
      ([url]) => url === "/api/v1/organizations/current/api-keys/key_01/rotate/",
    );
    expect(rotationCall).toBeDefined();
    if (!rotationCall) {
      throw new Error("A rotação não foi enviada.");
    }
    expect(JSON.parse(String((rotationCall[1] as RequestInit).body))).toEqual({
      overlap_seconds: 86400,
    });
  });

  it("revoga uma chave após confirmação e remove suas ações do inventário", async () => {
    const fetchMock = installFetchMock((input) => {
      if (String(input) === "/api/v1/organizations/current/") {
        return Promise.resolve(organizationDashboardResponse());
      }

      if (String(input) === "/api/v1/organizations/current/api-keys/") {
        return Promise.resolve(
          new Response(
            JSON.stringify({
              api_keys: [
                {
                  id: "key_01",
                  name: "Produção marketplace",
                  prefix: "esk_live_ab12",
                  scopes: ["agreements:write"],
                  expires_at: null,
                  last_used_at: null,
                  last_used_ip: null,
                  status: "ACTIVE",
                },
              ],
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        );
      }

      if (String(input) === "/api/v1/auth/csrf/") {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      return Promise.resolve(
        new Response(
          JSON.stringify({
            api_key: {
              id: "key_01",
              name: "Produção marketplace",
              prefix: "esk_live_ab12",
              scopes: ["agreements:write"],
              expires_at: null,
              last_used_at: null,
              last_used_ip: null,
              status: "REVOKED",
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });

    render(<OrganizationDashboard onLogout={() => undefined} onReturnToLogin={() => undefined} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Revogar Produção marketplace" })).toBeEnabled();
    });

    fireEvent.click(screen.getByRole("button", { name: "Revogar Produção marketplace" }));
    const confirmation = screen.getByRole("dialog", { name: "Revogar chave" });
    expect(confirmation).toHaveTextContent("não pode ser desfeita");
    fireEvent.click(within(confirmation).getByRole("button", { name: "Revogar chave" }));

    await waitFor(() => {
      expect(screen.getByText("Revogada")).toBeInTheDocument();
    });

    expect(
      fetchMock.mock.calls.some(
        ([url]) => url === "/api/v1/organizations/current/api-keys/key_01/revoke/",
      ),
    ).toBe(true);
    expect(screen.queryByRole("button", { name: "Rotacionar Produção marketplace" })).toBeNull();
  });

  it("bloqueia novas emissões quando a organização já tem duas chaves ativas", async () => {
    installFetchMock((input) => {
      if (String(input) === "/api/v1/organizations/current/") {
        return Promise.resolve(organizationDashboardResponse());
      }

      return Promise.resolve(
        new Response(
          JSON.stringify({
            api_keys: [
              {
                id: "key_01",
                name: "Marketplace A",
                prefix: "esk_live_aa11",
                scopes: ["agreements:read"],
                expires_at: null,
                last_used_at: null,
                last_used_ip: null,
                status: "ACTIVE",
              },
              {
                id: "key_02",
                name: "Marketplace B",
                prefix: "esk_live_bb22",
                scopes: ["agreements:write"],
                expires_at: null,
                last_used_at: null,
                last_used_ip: null,
                status: "ACTIVE",
              },
            ],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    });

    render(<OrganizationDashboard onLogout={() => undefined} onReturnToLogin={() => undefined} />);

    await waitFor(() => {
      expect(screen.getByText(/Limite de duas chaves ativas atingido/)).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: "Criar chave de integração" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Rotacionar Marketplace A" })).toBeDisabled();
  });
});
