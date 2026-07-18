import { useCallback, useEffect, useState } from "react";
import { ApiError, checkoutApi, type PublicCheckout } from "../../lib/api";

type CheckoutScreenProps = {
  token: string;
};

type CheckoutState =
  | { status: "loading" }
  | { status: "ready"; checkout: PublicCheckout }
  | { status: "not-found" }
  | { status: "error" };

type CheckoutStatus = {
  detail: string;
  label: string;
  tone: "held" | "pending" | "review" | "released";
};

const checkoutStatus: Record<string, CheckoutStatus> = {
  AWAITING_PAYMENT: {
    label: "Aguardando pagamento PIX",
    detail: "O acordo será enviado para análise assim que o pagamento for confirmado.",
    tone: "pending",
  },
  PENDING_FUNDING: {
    label: "Aguardando pagamento PIX",
    detail: "O acordo será enviado para análise assim que o pagamento for confirmado.",
    tone: "pending",
  },
  FUNDING_PROCESSING: {
    label: "Pagamento em análise",
    detail: "O pagamento foi recebido e está passando pela análise de segurança.",
    tone: "review",
  },
  PENDING_RISK_REVIEW: {
    label: "Pagamento em análise",
    detail: "O pagamento foi recebido e está passando pela análise de segurança.",
    tone: "review",
  },
  HELD: {
    label: "Valor protegido em custódia",
    detail: "O pagamento foi aprovado e permanece protegido até a confirmação de entrega.",
    tone: "held",
  },
  HELD_IN_ESCROW: {
    label: "Valor protegido em custódia",
    detail: "O pagamento foi aprovado e permanece protegido até a confirmação de entrega.",
    tone: "held",
  },
  RELEASED: {
    label: "Valor liberado",
    detail: "A entrega foi confirmada e o valor foi liberado para a organização.",
    tone: "released",
  },
};

const unknownStatus: CheckoutStatus = {
  label: "Acordo em processamento",
  detail: "Estamos atualizando os dados deste acordo protegido.",
  tone: "pending",
};

function checkoutStep(status: string): number {
  if (status === "RELEASED") {
    return 3;
  }
  if (status === "HELD_IN_ESCROW" || status === "HELD") {
    return 2;
  }
  if (status === "PENDING_RISK_REVIEW" || status === "FUNDING_PROCESSING") {
    return 1;
  }
  return 0;
}

function displayMoney(amount: string, currency: PublicCheckout["agreement"]["currency"]): string {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(amount);

  if (!match) {
    return "—";
  }

  const [, integer, decimal = ""] = match;
  const major = new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency,
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(BigInt(integer));

  return `${major},${decimal.padEnd(2, "0")}`;
}

function displayFee(feeBps: number): string {
  return new Intl.NumberFormat("pt-BR", {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(feeBps / 10_000);
}

function deliveryWindow(copy: PublicCheckout["agreement"]): string {
  if (copy.delivery_due_at) {
    return new Intl.DateTimeFormat("pt-BR", {
      dateStyle: "long",
      timeZone: "UTC",
    }).format(new Date(copy.delivery_due_at));
  }

  return `${copy.delivery_window_days} dias após a confirmação do pagamento`;
}

export function CheckoutScreen({ token }: CheckoutScreenProps) {
  const [state, setState] = useState<CheckoutState>({ status: "loading" });

  const loadCheckout = useCallback(async () => {
    setState({ status: "loading" });

    try {
      const checkout = await checkoutApi.get(token);
      setState({ status: "ready", checkout });
    } catch (error) {
      if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
        setState({ status: "not-found" });
        return;
      }

      setState({ status: "error" });
    }
  }, [token]);

  useEffect(() => {
    void loadCheckout();
  }, [loadCheckout]);

  if (state.status === "loading") {
    return <CheckoutLoading />;
  }

  if (state.status === "not-found") {
    return <CheckoutUnavailable kind="not-found" onRetry={loadCheckout} />;
  }

  if (state.status === "error") {
    return <CheckoutUnavailable kind="error" onRetry={loadCheckout} />;
  }

  const { agreement } = state.checkout;
  const status = checkoutStatus[agreement.status] ?? unknownStatus;
  const currentStep = checkoutStep(agreement.status);

  return (
    <main className="checkout-shell">
      <header className="checkout-topbar">
        <a href="/" aria-label="Escrow — início">
          <span className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <span>ESCROW</span>
          </span>
        </a>
        <p>CHECKOUT PROTEGIDO · PIX</p>
      </header>

      <section className="checkout-layout" aria-labelledby="checkout-title">
        <div className="checkout-introduction">
          <p className="eyebrow">PAGAMENTO COM CUSTÓDIA</p>
          <h1 id="checkout-title">Revise seu pagamento</h1>
          <p className="checkout-lede">
            Seu pagamento fica protegido em custódia. A organização só recebe o valor depois da
            confirmação da entrega.
          </p>

          <ol className="checkout-steps" aria-label="Etapas do pagamento protegido">
            <li className={currentStep >= 0 ? "is-current" : undefined}>1. Pagamento PIX</li>
            <li className={currentStep >= 1 ? "is-current" : undefined}>2. Análise</li>
            <li className={currentStep >= 2 ? "is-current" : undefined}>3. Custódia</li>
            <li className={currentStep >= 3 ? "is-current" : undefined}>4. Liberação</li>
          </ol>
        </div>

        <section className="checkout-card" aria-label="Resumo do acordo de custódia">
          <div className="checkout-amount">
            <span>VALOR DO ACORDO</span>
            <output>{displayMoney(agreement.amount, agreement.currency)}</output>
          </div>

          <div className={`checkout-status checkout-status-${status.tone}`} role="status">
            <span className="checkout-status-mark" aria-hidden="true" />
            <div>
              <strong>{status.label}</strong>
              <p>{status.detail}</p>
            </div>
          </div>

          <dl className="checkout-details">
            <div>
              <dt>Pagador</dt>
              <dd>{agreement.customer.name}</dd>
            </div>
            <div>
              <dt>E-mail</dt>
              <dd>{agreement.customer.email_masked}</dd>
            </div>
            <div>
              <dt>Documento</dt>
              <dd>{agreement.customer.document_masked}</dd>
            </div>
            <div>
              <dt>Prazo para entrega</dt>
              <dd>{deliveryWindow(agreement)}</dd>
            </div>
            <div>
              <dt>Taxa de custódia</dt>
              <dd>{displayFee(agreement.fee_bps)}</dd>
            </div>
            <div>
              <dt>Referência</dt>
              <dd>{agreement.id}</dd>
            </div>
          </dl>

          <p className="checkout-disclosure">
            A cobrança PIX será apresentada nesta tela quando estiver pronta. Não compartilhe este
            link com terceiros.
          </p>
        </section>
      </section>
    </main>
  );
}

function CheckoutLoading() {
  return (
    <main className="checkout-shell checkout-center" role="status" aria-live="polite">
      <p className="eyebrow">CHECKOUT PROTEGIDO</p>
      <h1>Carregando checkout seguro</h1>
      <p>Confirmando os detalhes do acordo de custódia.</p>
    </main>
  );
}

function CheckoutUnavailable({
  kind,
  onRetry,
}: {
  kind: "error" | "not-found";
  onRetry: () => Promise<void>;
}) {
  const notFound = kind === "not-found";

  return (
    <main className="checkout-shell checkout-center" role="alert" aria-live="assertive">
      <p className="eyebrow">CHECKOUT PROTEGIDO</p>
      <h1>
        {notFound ? "Este checkout não está disponível." : "Não foi possível abrir este checkout."}
      </h1>
      <p>
        {notFound
          ? "O link pode ter expirado ou já ter sido utilizado. Solicite um novo link à organização."
          : "Tente novamente em alguns instantes. Seus dados e seu pagamento continuam protegidos."}
      </p>
      {!notFound ? (
        <button
          className="primary-action checkout-retry"
          type="button"
          onClick={() => void onRetry()}
        >
          Tentar novamente
        </button>
      ) : null}
    </main>
  );
}
