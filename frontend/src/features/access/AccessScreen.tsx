import { type FormEvent, useState } from "react";
import { ApiError, organizationApi } from "../../lib/api";

export type AccessRoute = "/login" | "/recuperar" | "/redefinir-senha" | "/registro";

type AccessScreenProps = {
  mode: AccessRoute;
  onAuthenticated: () => void;
  onNavigate: (route: AccessRoute | "/") => void;
};

type Feedback = {
  tone: "error" | "success";
  message: string;
};

const accessContent: Record<
  AccessRoute,
  {
    eyebrow: string;
    title: string;
    description: string;
  }
> = {
  "/registro": {
    eyebrow: "NOVO ESPAÇO OPERACIONAL",
    title: "Organize a custódia desde o primeiro acordo.",
    description:
      "Crie o espaço da sua organização. O acesso inicial recebe o papel de proprietária.",
  },
  "/login": {
    eyebrow: "ACESSO DA ORGANIZAÇÃO",
    title: "Volte ao seu posto de controle.",
    description: "Entre para acompanhar os valores em custódia e as liberações da sua organização.",
  },
  "/recuperar": {
    eyebrow: "RECUPERAÇÃO DE ACESSO",
    title: "Recupere o controle sem expor sua conta.",
    description:
      "Enviaremos orientações para o e-mail informado, caso ele pertença a uma conta registrada.",
  },
  "/redefinir-senha": {
    eyebrow: "NOVA SENHA",
    title: "Defina uma credencial nova e exclusiva.",
    description:
      "O link de recuperação confirma sua solicitação. Escolha uma senha forte antes de voltar ao painel.",
  },
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  return "A operação não respondeu. Verifique sua conexão e tente novamente.";
}

function AccessLink({
  children,
  onNavigate,
  to,
}: {
  children: string;
  onNavigate: AccessScreenProps["onNavigate"];
  to: AccessRoute | "/";
}) {
  return (
    <a
      href={to}
      onClick={(event) => {
        event.preventDefault();
        onNavigate(to);
      }}
    >
      {children}
    </a>
  );
}

export function AccessScreen({ mode, onAuthenticated, onNavigate }: AccessScreenProps) {
  const content = accessContent[mode];
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<Feedback | null>(null);

  async function submitRegistration(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new window.FormData(event.currentTarget);
    const password = String(form.get("password") ?? "");
    const passwordConfirmation = String(form.get("password_confirmation") ?? "");

    if (password.length < 12) {
      setFeedback({ tone: "error", message: "Use uma senha com ao menos 12 caracteres." });
      return;
    }

    if (password !== passwordConfirmation) {
      setFeedback({ tone: "error", message: "As senhas precisam ser idênticas." });
      return;
    }

    setFeedback(null);
    setIsSubmitting(true);

    try {
      await organizationApi.register({
        organization_name: String(form.get("organization_name") ?? ""),
        email: String(form.get("email") ?? ""),
        password,
        password_confirmation: passwordConfirmation,
      });
      onAuthenticated();
    } catch (error) {
      setFeedback({ tone: "error", message: errorMessage(error) });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function submitLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new window.FormData(event.currentTarget);
    setFeedback(null);
    setIsSubmitting(true);

    try {
      await organizationApi.login({
        email: String(form.get("email") ?? ""),
        password: String(form.get("password") ?? ""),
      });
      onAuthenticated();
    } catch (error) {
      setFeedback({ tone: "error", message: errorMessage(error) });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function submitRecovery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new window.FormData(event.currentTarget);
    setFeedback(null);
    setIsSubmitting(true);

    try {
      await organizationApi.recoverPassword(String(form.get("email") ?? ""));
      setFeedback({
        tone: "success",
        message:
          "Se houver uma conta para este e-mail, as orientações de recuperação foram enviadas.",
      });
    } catch (error) {
      setFeedback({ tone: "error", message: errorMessage(error) });
    } finally {
      setIsSubmitting(false);
    }
  }

  async function submitPasswordReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new window.FormData(event.currentTarget);
    const password = String(form.get("password") ?? "");
    const passwordConfirmation = String(form.get("password_confirmation") ?? "");
    const parameters = new URLSearchParams(window.location.search);
    const uid = parameters.get("uid");
    const token = parameters.get("token");

    if (!uid || !token) {
      setFeedback({ tone: "error", message: "Este link de recuperação é inválido ou expirou." });
      return;
    }
    if (password.length < 12) {
      setFeedback({ tone: "error", message: "Use uma senha com ao menos 12 caracteres." });
      return;
    }
    if (password !== passwordConfirmation) {
      setFeedback({ tone: "error", message: "As senhas precisam ser idênticas." });
      return;
    }

    setFeedback(null);
    setIsSubmitting(true);
    try {
      await organizationApi.confirmPasswordRecovery({
        uid,
        token,
        password,
        password_confirmation: passwordConfirmation,
      });
      setFeedback({
        tone: "success",
        message: "Senha atualizada. Agora você pode entrar no seu espaço de custódia.",
      });
    } catch (error) {
      setFeedback({ tone: "error", message: errorMessage(error) });
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="access-shell">
      <header className="access-topbar">
        <AccessLink onNavigate={onNavigate} to="/">
          ESCROW
        </AccessLink>
        <p>AMBIENTE LOCAL · SIMULAÇÃO</p>
      </header>

      <section className="access-grid" aria-labelledby="access-title">
        <div className="access-introduction">
          <p className="eyebrow">{content.eyebrow}</p>
          <h1 id="access-title">{content.title}</h1>
          <p>{content.description}</p>

          <ol className="access-rail" aria-label="Etapas de acesso à organização">
            <li className={mode === "/registro" ? "is-current" : ""}>1. Credenciais</li>
            <li>2. Organização</li>
            <li>3. Custódia</li>
          </ol>
        </div>

        <section className="access-card" aria-label={content.eyebrow}>
          {mode === "/registro" ? (
            <form className="auth-form" onSubmit={submitRegistration} noValidate>
              <div className="form-heading">
                <p>PRIMEIRO ACESSO</p>
                <h2>Abra seu espaço</h2>
              </div>

              <label>
                Nome da organização
                <input name="organization_name" autoComplete="organization" required />
              </label>
              <label>
                E-mail de trabalho
                <input name="email" type="email" autoComplete="email" required />
              </label>
              <label>
                Senha
                <input
                  name="password"
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  aria-describedby="password-guidance"
                  required
                />
              </label>
              <p id="password-guidance" className="field-guidance">
                Mínimo de 12 caracteres. Senhas conhecidas em vazamentos são recusadas.
              </p>
              <label>
                Confirme a senha
                <input
                  name="password_confirmation"
                  type="password"
                  autoComplete="new-password"
                  required
                />
              </label>

              <FormFeedback feedback={feedback} />
              <button className="primary-action" type="submit" disabled={isSubmitting}>
                {isSubmitting ? "Criando espaço…" : "Criar espaço seguro"}
              </button>
              <p className="form-footer">
                Já tem acesso?{" "}
                <AccessLink onNavigate={onNavigate} to="/login">
                  Entrar
                </AccessLink>
              </p>
            </form>
          ) : null}

          {mode === "/login" ? (
            <form className="auth-form" onSubmit={submitLogin} noValidate>
              <div className="form-heading">
                <p>SESSÃO PROTEGIDA</p>
                <h2>Entre na organização</h2>
              </div>

              <label>
                E-mail de trabalho
                <input name="email" type="email" autoComplete="email" required />
              </label>
              <label>
                Senha
                <input name="password" type="password" autoComplete="current-password" required />
              </label>

              <div className="form-row">
                <AccessLink onNavigate={onNavigate} to="/recuperar">
                  Esqueci minha senha
                </AccessLink>
              </div>
              <FormFeedback feedback={feedback} />
              <button className="primary-action" type="submit" disabled={isSubmitting}>
                {isSubmitting ? "Entrando…" : "Entrar com segurança"}
              </button>
              <p className="form-footer">
                Ainda não usa o Escrow?{" "}
                <AccessLink onNavigate={onNavigate} to="/registro">
                  Criar organização
                </AccessLink>
              </p>
            </form>
          ) : null}

          {mode === "/recuperar" ? (
            <form className="auth-form" onSubmit={submitRecovery} noValidate>
              <div className="form-heading">
                <p>EMAIL DE RECUPERAÇÃO</p>
                <h2>Redefina sua senha</h2>
              </div>

              <label>
                E-mail de trabalho
                <input name="email" type="email" autoComplete="email" required />
              </label>
              <FormFeedback feedback={feedback} />
              <button className="primary-action" type="submit" disabled={isSubmitting}>
                {isSubmitting ? "Enviando…" : "Enviar orientações"}
              </button>
              <p className="form-footer">
                Lembrou sua senha?{" "}
                <AccessLink onNavigate={onNavigate} to="/login">
                  Voltar para entrar
                </AccessLink>
              </p>
            </form>
          ) : null}

          {mode === "/redefinir-senha" ? (
            <form className="auth-form" onSubmit={submitPasswordReset} noValidate>
              <div className="form-heading">
                <p>LINK DE RECUPERAÇÃO</p>
                <h2>Atualize sua senha</h2>
              </div>

              <label>
                Nova senha
                <input
                  name="password"
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  required
                />
              </label>
              <label>
                Confirme a nova senha
                <input
                  name="password_confirmation"
                  type="password"
                  autoComplete="new-password"
                  minLength={12}
                  required
                />
              </label>
              <FormFeedback feedback={feedback} />
              <button className="primary-action" type="submit" disabled={isSubmitting}>
                {isSubmitting ? "Atualizando…" : "Atualizar senha"}
              </button>
              <p className="form-footer">
                Já atualizou?{" "}
                <AccessLink onNavigate={onNavigate} to="/login">
                  Entrar
                </AccessLink>
              </p>
            </form>
          ) : null}
        </section>
      </section>
    </main>
  );
}

function FormFeedback({ feedback }: { feedback: Feedback | null }) {
  if (!feedback) {
    return null;
  }

  return (
    <p
      className={`form-feedback form-feedback-${feedback.tone}`}
      role={feedback.tone === "error" ? "alert" : "status"}
    >
      {feedback.message}
    </p>
  );
}
