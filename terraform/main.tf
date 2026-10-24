# Azure layout for the Quillmoor Market AI platform. DESCRIPTIVE ONLY - applying costs money.
resource "azurerm_resource_group" "rg" {
  name     = "${var.prefix}-rg"
  location = var.location
}

resource "azurerm_virtual_network" "vnet" {
  name                = "${var.prefix}-vnet"
  address_space       = ["10.20.0.0/16"]
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

# edge / app / data tiers mirror the compose networks
resource "azurerm_subnet" "edge" {
  name                 = "edge"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.20.1.0/24"]
}

resource "azurerm_subnet" "app" {
  name                 = "app"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.20.2.0/23"]
}

resource "azurerm_subnet" "data" {
  name                 = "data"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.20.4.0/24"]
}

resource "azurerm_network_security_group" "data" {
  name                = "${var.prefix}-data-nsg"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  security_rule {
    name                       = "allow-app-only"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_address_prefix      = "10.20.2.0/23"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_ranges    = ["5432", "6379", "9000"]
  }

  security_rule {
    name                       = "deny-all-else"
    priority                   = 4000
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_address_prefix      = "*"
    source_port_range          = "*"
    destination_address_prefix = "*"
    destination_port_range     = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "data" {
  subnet_id                 = azurerm_subnet.data.id
  network_security_group_id = azurerm_network_security_group.data.id
}

resource "azurerm_kubernetes_cluster" "aks" {
  name                = "${var.prefix}-aks"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  dns_prefix          = var.prefix

  default_node_pool {
    name           = "system"
    node_count     = var.node_count
    vm_size        = "Standard_D2s_v5"
    vnet_subnet_id = azurerm_subnet.app.id
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin = "azure"
    network_policy = "azure"
  }
}

resource "azurerm_postgresql_flexible_server" "db" {
  name                          = "${var.prefix}-pg"
  resource_group_name           = azurerm_resource_group.rg.name
  location                      = azurerm_resource_group.rg.location
  version                       = "16"
  sku_name                      = "B_Standard_B1ms"
  storage_mb                    = 32768
  public_network_access_enabled = false
  # admin credentials would come from Key Vault at apply time; none are stored here
}

resource "azurerm_redis_cache" "cache" {
  name                          = "${var.prefix}-redis"
  location                      = azurerm_resource_group.rg.location
  resource_group_name           = azurerm_resource_group.rg.name
  capacity                      = 0
  family                        = "C"
  sku_name                      = "Basic"
  minimum_tls_version           = "1.2"
  public_network_access_enabled = false
}

resource "azurerm_storage_account" "objects" {
  name                            = "${var.prefix}objects"
  resource_group_name             = azurerm_resource_group.rg.name
  location                        = azurerm_resource_group.rg.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
}
