# EKS Infrastructure as Code (Terraform)

Defines the cloud infrastructure to run the routing service on **AWS EKS** -
the managed-cloud equivalent of the local `kind` deployment in
[`serving/k8s/`](../serving/k8s/). Provisions a VPC (public/private subnets,
NAT) and an EKS cluster with a managed node group.

> **Not applied here.** A live EKS cluster and its nodes bill by the hour, so
> this is provided as validated code, not a running cluster. Apply it against
> your own AWS account when you want the cloud deployment.

## Verified

Validated without touching AWS (no credentials, no cost):

```
$ terraform init      # downloads the aws provider + vpc/eks modules
$ terraform validate
Success! The configuration is valid.
$ terraform fmt -check
# clean
```

## What it provisions

| Resource | Detail |
|---|---|
| VPC | 10.0.0.0/16, 2 AZs, public + private subnets, single NAT gateway |
| EKS cluster | version 1.31, public API endpoint |
| Node group | managed, `t3.medium`, 2-4 nodes (desired 2) |

Subnets are tagged for EKS load-balancer discovery.

## Apply (needs an AWS account)

```bash
export AWS_PROFILE=your-profile          # credentials with EKS/VPC permissions
terraform init
terraform apply                          # creates the cluster (billable)
aws eks update-kubeconfig --region ap-northeast-2 --name leo-router
kubectl apply -f ../serving/k8s/deployment.yaml
terraform destroy                        # tear it down to stop billing
```

The same container image and Kubernetes manifests from `serving/` deploy
unchanged onto the resulting cluster - kind for local, EKS for cloud.

## Files

```
terraform/
  versions.tf     terraform + aws provider constraints
  variables.tf    region, cluster name, k8s version
  main.tf         VPC + EKS (community modules)
  outputs.tf      cluster name, endpoint, kubeconfig command
```
