import { afterEach, describe, expect, it, mock } from "bun:test";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "./App";

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
      balances: {
        held_brl_minor: 5000000,
        available_brl_minor: 245000,
      },
      upcoming_releases: [
        {
          id: "agr_01",
          gross_minor: 5000000,
          fee_minor: 100000,
          net_minor: 4900000,
          currency: "BRL",
          release_at: "2026-07-25T12:00:00Z",
        },
      ],
      api_keys: [],
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  cleanup();
  window.history.pushState({}, "", "/");
  setCookie("escrow_csrf=; Max-Age=0; path=/");
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

describe("acesso da organização", () => {
  it("permite que uma proprietária se cadastre e entre no espaço isolado", async () => {
    const fetchMock = installFetchMock((input) => {
      if (String(input).includes("/auth/csrf/")) {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      if (String(input).includes("/auth/register/")) {
        return Promise.resolve(
          new Response(JSON.stringify({ status: "registered" }), {
            status: 201,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }

      return Promise.resolve(organizationDashboardResponse());
    });
    window.history.pushState({}, "", "/registro");

    render(<App />);

    fireEvent.change(screen.getByLabelText("Nome da organização"), {
      target: { value: "Loja Horizonte" },
    });
    fireEvent.change(screen.getByLabelText("E-mail de trabalho"), {
      target: { value: "owner@horizonte.test" },
    });
    fireEvent.change(screen.getByLabelText("Senha"), {
      target: { value: "UmaSenhaLonga#2026" },
    });
    fireEvent.change(screen.getByLabelText("Confirme a senha"), {
      target: { value: "UmaSenhaLonga#2026" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Criar espaço seguro" }));

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/auth/csrf/")).toBe(true);
      expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/auth/register/")).toBe(true);
    });

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Loja Horizonte" })).toBeInTheDocument();
    });

    expect(screen.getByText("OWNER")).toBeInTheDocument();
    expect(screen.getByText("Dados somente desta organização")).toBeInTheDocument();
    expect(screen.getAllByText(/50\.000,00/)).not.toHaveLength(0);
    const releaseBreakdown = screen.getByText(/Bruto/);
    expect(releaseBreakdown).toHaveTextContent("Taxa");
    expect(releaseBreakdown).toHaveTextContent("Líquido");
  });

  it("exige confirmação idêntica antes de enviar uma nova senha", () => {
    const fetchMock = installFetchMock(() => Promise.reject(new Error("não deve chamar a API")));
    window.history.pushState({}, "", "/registro");

    render(<App />);

    fireEvent.change(screen.getByLabelText("Senha"), { target: { value: "UmaSenhaLonga#2026" } });
    fireEvent.change(screen.getByLabelText("Confirme a senha"), {
      target: { value: "OutraSenhaLonga#2026" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Criar espaço seguro" }));

    expect(screen.getByRole("alert")).toHaveTextContent("As senhas precisam ser idênticas.");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("mostra o motivo de uma senha recusada pela API", async () => {
    installFetchMock((input) => {
      if (String(input).includes("/auth/csrf/")) {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      return Promise.resolve(
        new Response(
          JSON.stringify({
            code: "validation_error",
            errors: { password: ["Esta senha aparece em vazamentos conhecidos. Escolha outra."] },
          }),
          { status: 400, headers: { "Content-Type": "application/json" } },
        ),
      );
    });
    window.history.pushState({}, "", "/registro");

    render(<App />);

    fireEvent.change(screen.getByLabelText("Nome da organização"), {
      target: { value: "Loja Horizonte" },
    });
    fireEvent.change(screen.getByLabelText("E-mail de trabalho"), {
      target: { value: "owner@horizonte.test" },
    });
    fireEvent.change(screen.getByLabelText("Senha"), {
      target: { value: "UmaSenhaLonga#2026" },
    });
    fireEvent.change(screen.getByLabelText("Confirme a senha"), {
      target: { value: "UmaSenhaLonga#2026" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Criar espaço seguro" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Esta senha aparece em vazamentos conhecidos. Escolha outra.",
      );
    });
  });

  it("permite que um membro entre com uma sessão protegida", async () => {
    const fetchMock = installFetchMock((input) => {
      if (String(input).includes("/auth/csrf/")) {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      if (String(input).includes("/auth/login/")) {
        return Promise.resolve(
          new Response(
            JSON.stringify({ user: { id: "user_01", email: "finance@horizonte.test" } }),
            {
              status: 200,
              headers: { "Content-Type": "application/json" },
            },
          ),
        );
      }

      return Promise.resolve(organizationDashboardResponse());
    });
    window.history.pushState({}, "", "/login");

    render(<App />);

    fireEvent.change(screen.getByLabelText("E-mail de trabalho"), {
      target: { value: "finance@horizonte.test" },
    });
    fireEvent.change(screen.getByLabelText("Senha"), { target: { value: "UmaSenhaLonga#2026" } });
    fireEvent.click(screen.getByRole("button", { name: "Entrar com segurança" }));

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Loja Horizonte" })).toBeInTheDocument();
    });

    const loginCall = fetchMock.mock.calls.find(([url]) => url === "/api/v1/auth/login/");
    expect(loginCall).toBeDefined();
    const loginOptions = loginCall?.[1] as RequestInit;
    expect(JSON.parse(String(loginOptions.body))).toEqual({
      email: "finance@horizonte.test",
      password: "UmaSenhaLonga#2026",
    });
    expect(new Headers(loginOptions.headers).get("X-CSRFToken")).toBe("teste-csrf");
  });

  it("solicita recuperação sem revelar se o e-mail pertence a uma conta", async () => {
    const fetchMock = installFetchMock((input) => {
      if (String(input).includes("/auth/csrf/")) {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      return Promise.resolve(
        new Response(JSON.stringify({ status: "accepted" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    window.history.pushState({}, "", "/recuperar");

    render(<App />);

    fireEvent.change(screen.getByLabelText("E-mail de trabalho"), {
      target: { value: "unknown@horizonte.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Enviar orientações" }));

    await waitFor(() => {
      expect(
        screen.getByText(
          "Se houver uma conta para este e-mail, as orientações de recuperação foram enviadas.",
        ),
      ).toBeInTheDocument();
    });

    const recoveryCall = fetchMock.mock.calls.find(
      ([url]) => url === "/api/v1/auth/password-recovery/",
    );
    expect(recoveryCall).toBeDefined();
    if (!recoveryCall) {
      throw new Error("A recuperação não foi enviada.");
    }
    expect(JSON.parse(String((recoveryCall[1] as RequestInit).body))).toEqual({
      email: "unknown@horizonte.test",
    });
  });

  it("aceita a nova senha somente pelo link opaco de recuperação", async () => {
    const fetchMock = installFetchMock((input) => {
      if (String(input).includes("/auth/csrf/")) {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      return Promise.resolve(
        new Response(JSON.stringify({ status: "password_updated" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });
    window.history.pushState({}, "", "/redefinir-senha/?uid=dXNlcg&token=opaque-token");
    expect(window.location.pathname).toBe("/redefinir-senha/");

    render(<App />);

    fireEvent.change(screen.getByLabelText("Nova senha"), {
      target: { value: "UmaSenhaNovaLonga#2026" },
    });
    fireEvent.change(screen.getByLabelText("Confirme a nova senha"), {
      target: { value: "UmaSenhaNovaLonga#2026" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Atualizar senha" }));

    await waitFor(() => {
      expect(
        screen.getByText("Senha atualizada. Agora você pode entrar no seu espaço de custódia."),
      ).toBeInTheDocument();
    });

    const confirmationCall = fetchMock.mock.calls.find(
      ([url]) => url === "/api/v1/auth/password-recovery/confirm/",
    );
    expect(confirmationCall).toBeDefined();
    if (!confirmationCall) {
      throw new Error("A confirmação de senha não foi enviada.");
    }
    expect(JSON.parse(String((confirmationCall[1] as RequestInit).body))).toEqual({
      uid: "dXNlcg",
      token: "opaque-token",
      password: "UmaSenhaNovaLonga#2026",
      password_confirmation: "UmaSenhaNovaLonga#2026",
    });
  });

  it("encerra a sessão antes de voltar para a tela de entrada", async () => {
    const fetchMock = installFetchMock((input) => {
      if (String(input).includes("/auth/csrf/")) {
        setCookie("escrow_csrf=teste-csrf; path=/");
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      if (String(input).includes("/auth/logout/")) {
        return Promise.resolve(new Response(null, { status: 204 }));
      }

      return Promise.resolve(organizationDashboardResponse());
    });
    window.history.pushState({}, "", "/dashboard");

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Loja Horizonte" })).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "Sair" }));

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Volte ao seu posto de controle." }),
      ).toBeInTheDocument();
    });

    expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/auth/csrf/")).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => url === "/api/v1/auth/logout/")).toBe(true);
  });
});
