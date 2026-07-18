import { afterEach, describe, expect, it, mock } from "bun:test";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import App from "../../App";

const originalFetch = globalThis.fetch;

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

function checkoutResponse() {
  return new Response(
    JSON.stringify({
      agreement: {
        id: "agr_7Nf93",
        status: "AWAITING_PAYMENT",
        customer: {
          name: "Marina Silva",
          email_masked: "ma••••@exemplo.com",
          document_masked: "***.456.789-**",
        },
        amount: "50000.00",
        currency: "BRL",
        delivery_window_days: 7,
        delivery_due_at: null,
        fee_bps: 250,
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  cleanup();
  window.history.pushState({}, "", "/");
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: originalFetch,
    writable: true,
  });
  mock.restore();
});

describe("checkout público", () => {
  it("abre um acordo sem sessão e apresenta somente os dados do checkout", async () => {
    const fetchMock = installFetchMock(() => Promise.resolve(checkoutResponse()));
    window.history.pushState({}, "", "/checkout/checkout-token-publico");

    render(<App />);

    expect(screen.getByRole("status")).toHaveTextContent("Carregando checkout seguro");

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Revise seu pagamento" })).toBeInTheDocument();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/checkout/checkout-token-publico/",
      expect.objectContaining({ cache: "no-store", credentials: "omit", method: "GET" }),
    );
    expect(screen.getByText(/R\$\s*50\.000,00/)).toBeInTheDocument();
    expect(screen.getByText("Marina Silva")).toBeInTheDocument();
    expect(screen.getByText("ma••••@exemplo.com")).toBeInTheDocument();
    expect(screen.getByText("***.456.789-**")).toBeInTheDocument();
    expect(screen.getByText("Aguardando pagamento PIX")).toBeInTheDocument();
    expect(screen.getByText("7 dias após a confirmação do pagamento")).toBeInTheDocument();
  });

  it("explica quando o link público não existe ou expirou", async () => {
    installFetchMock(() =>
      Promise.resolve(
        new Response(JSON.stringify({ code: "checkout_not_found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    window.history.pushState({}, "", "/checkout/link-inexistente");

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Este checkout não está disponível." }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/solicite um novo link/i)).toBeInTheDocument();
  });

  it("não expõe detalhes internos quando o checkout falha", async () => {
    installFetchMock(() => Promise.resolve(new Response("falha interna", { status: 500 })));
    window.history.pushState({}, "", "/checkout/checkout-com-erro");

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Não foi possível abrir este checkout." }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/tente novamente em alguns instantes/i)).toBeInTheDocument();
    expect(screen.queryByText("falha interna")).not.toBeInTheDocument();
  });

  it("trata HTML de proxy mal configurado como falha segura", async () => {
    installFetchMock(() => Promise.resolve(new Response("<html>checkout</html>", { status: 200 })));
    window.history.pushState({}, "", "/checkout/resposta-invalida");

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Não foi possível abrir este checkout." }),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("<html>checkout</html>")).not.toBeInTheDocument();
  });

  it("mapeia estados canônicos de processamento e custódia", async () => {
    installFetchMock(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            agreement: {
              id: "agr-held",
              status: "HELD",
              customer: {
                name: "Marina Silva",
                email_masked: "ma••••@exemplo.com",
                document_masked: "***.456.789-**",
              },
              amount: "50000.00",
              currency: "BRL",
              delivery_window_days: 7,
              delivery_due_at: null,
              fee_bps: 200,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    window.history.pushState({}, "", "/checkout/acordo-held");

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText("Valor protegido em custódia")).toBeInTheDocument();
    });
    expect(screen.getByText("3. Custódia")).toHaveClass("is-current");
  });
});
