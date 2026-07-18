# MiniStack bootstrap

This module creates the local AWS-compatible resources used by the identity
path: an SES sender identity, a password-recovery template, one KMS key, and
one Secrets Manager secret payload. It intentionally does not create S3
resources: private evidence belongs to Ceph RGW.

## Local use

Start MiniStack, then create a local variable file from the example and apply:

```sh
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
terraform -chdir=terraform init
terraform -chdir=terraform apply
```

MiniStack treats the local 12-digit access key as its account identifier. The
fictional `000000000000` value in the example keeps Terraform, KMS, and the API
inside the same local account.

`terraform.tfstate`, `terraform.tfvars`, and provider cache files are ignored
because the local state can contain the simulated secret payload. Terraform's
local backend is an MVP decision only. A real AWS environment must use remote,
locked state and separately configure IAM, networking, verified SES identities,
credentials, and emulator-gap validation.

## Future AWS endpoint

Set `ministack_endpoint = null`, choose the target AWS region and identity, and
provide credentials through the AWS provider credential chain (or the two
variables). This switches service endpoints; it is not a complete production
migration.
