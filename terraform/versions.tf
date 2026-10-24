terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.10"
    }
  }
  # No backend on purpose: this layout is validated in CI, never applied.
}

provider "azurerm" {
  features {}
  skip_provider_registration = true
}
