import { useCallback, useEffect, useState } from "react";
import "./styles.css";

type ReadinessState = "checking" | "ready" | "degraded";

type ReadinessCopy = {
  detail: string;
  label: string;
};

const readinessCopy: Record<ReadinessState, ReadinessCopy> = {
  checking: {
    label: "Verificando operação",
    detail: "Consultando a prontidão dos serviços que sustentam a custódia.",
  },
  ready: {
    label: "Operação normal",
    detail: "A API está pronta para receber novos acordos de custódia.",
  },
  degraded: {
    label: "Atenção operacional",
    detail: "Não foi possível confirmar a prontidão. Verifique o serviço e tente novamente.",
  },
};

function hasReadyStatus(payload: unknown): payload is { status: "ready" } {
  return (
    typeof payload === "object" &&
    payload !== null &&
    "status" in payload &&
    payload.status === "ready"
  );
}

export default function App() {
  const [readiness, setReadiness] = useState<ReadinessState>("checking");

  const checkReadiness = useCallback(async () => {
    setReadiness("checking");

    try {
      const response = await fetch("/health/ready/", {
        headers: { Accept: "application/json" },
      });
      const payload: unknown = await response.json();

      if (!response.ok || !hasReadyStatus(payload)) {
        throw new Error("Readiness check did not report a healthy service.");
      }

      setReadiness("ready");
    } catch {
      setReadiness("degraded");
    }
  }, []);

  useEffect(() => {
    void checkReadiness();
  }, [checkReadiness]);

  const status = readinessCopy[readiness];

  return (
    <main className="app-shell">
      <a className="skip-link" href="#operation-status">
        Ir para o estado operacional
      </a>

      <header className="topbar">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span>ESCROW</span>
        </div>
        <p className="environment-label">AMBIENTE LOCAL · SIMULAÇÃO</p>
      </header>

      <section className="control-room" aria-labelledby="shell-title">
        <div className="introduction">
          <p className="eyebrow">CUSTÓDIA OPERACIONAL</p>
          <h1 id="shell-title">Dinheiro em trânsito, sinais sob controle.</h1>
          <p className="lede">
            O console de operação acompanha cada acordo entre pagamento, análise e liberação. Comece
            confirmando que a infraestrutura está pronta.
          </p>

          <section className="custody-rail" aria-labelledby="custody-rail-title">
            <div className="rail-heading">
              <p id="custody-rail-title">Trilha de custódia</p>
              <span>ESTADO DO CICLO</span>
            </div>
            <ol>
              <li>
                <span className="rail-node rail-node-active" aria-hidden="true" />
                <strong>Acordo</strong>
                <small>registrado</small>
              </li>
              <li>
                <span className="rail-node" aria-hidden="true" />
                <strong>Análise</strong>
                <small>de risco</small>
              </li>
              <li>
                <span className="rail-node" aria-hidden="true" />
                <strong>Custódia</strong>
                <small>confirmada</small>
              </li>
              <li>
                <span className="rail-node" aria-hidden="true" />
                <strong>Liberação</strong>
                <small>autorizada</small>
              </li>
            </ol>
          </section>
        </div>

        <aside id="operation-status" className="status-panel" aria-labelledby="status-panel-title">
          <div className="panel-kicker">
            <span>01</span>
            <p>PRONTIDÃO DA PLATAFORMA</p>
          </div>
          <h2 id="status-panel-title">Antes de iniciar</h2>
          <p className="panel-intro">
            A custódia só aceita novos eventos quando seus serviços essenciais respondem.
          </p>

          <div
            className={`readiness-card readiness-card-${readiness}`}
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            <span className="status-indicator" aria-hidden="true" />
            <div>
              <p>{status.label}</p>
              <span>{status.detail}</span>
            </div>
          </div>

          <dl className="status-metadata">
            <div>
              <dt>Verificação</dt>
              <dd>/health/ready/</dd>
            </div>
            <div>
              <dt>Canal</dt>
              <dd>Infraestrutura local</dd>
            </div>
          </dl>

          <button type="button" className="refresh-button" onClick={() => void checkReadiness()}>
            Verificar novamente
          </button>
        </aside>
      </section>
    </main>
  );
}
