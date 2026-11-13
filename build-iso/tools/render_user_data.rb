#!/usr/bin/env ruby
# frozen_string_literal: true

require "yaml"

template_path, manifest_path, output_path = ARGV

unless template_path && manifest_path && output_path
  warn "Usage: render_user_data.rb TEMPLATE MANIFEST OUTPUT"
  exit 2
end

def load_yaml(path)
  YAML.safe_load_file(
    path,
    permitted_classes: [Symbol],
    aliases: true
  ) || {}
end

def deep_merge(base, override)
  override.each_pair do |key, value|
    base_value = base[key]
    base[key] =
      if base_value.is_a?(Hash) && value.is_a?(Hash)
        deep_merge(base_value, value)
      else
        value
      end
  end
  base
end

template = load_yaml(template_path)
manifest = load_yaml(manifest_path)

autoinstall_overrides = manifest.dig("user_data", "autoinstall") || {}
unless autoinstall_overrides.is_a?(Hash)
  warn "manifest user_data.autoinstall must be a mapping"
  exit 1
end

template["autoinstall"] ||= {}
deep_merge(template["autoinstall"], autoinstall_overrides)

# Subiquity >= 24.04.5 rejects reserved usernames such as "admin" in
# autoinstall `identity` ("Username is reserved by the system") and aborts the
# install. Keep `identity` as the declarative source in the template/manifest,
# but hand the account to cloud-init on the installed system instead; subiquity
# allows omitting `identity` when `user-data` is present.
identity = template["autoinstall"].delete("identity")
if identity.is_a?(Hash) && identity["username"]
  user_data = (template["autoinstall"]["user-data"] ||= {})
  user = {
    "name" => identity["username"],
    "gecos" => identity["realname"] || identity["username"],
    "groups" => %w[adm cdrom dip plugdev sudo],
    "shell" => "/bin/bash",
    "lock_passwd" => false,
  }
  user["passwd"] = identity["password"] if identity["password"]
  user_data["users"] = [user] + Array(user_data["users"]).reject { |u| u.is_a?(Hash) && u["name"] == user["name"] }
  if identity["hostname"]
    user_data["hostname"] ||= identity["hostname"]
    user_data["preserve_hostname"] = false unless user_data.key?("preserve_hostname")
  end
  ssh = template["autoinstall"]["ssh"]
  user_data["ssh_pwauth"] = ssh["allow-pw"] if ssh.is_a?(Hash) && ssh.key?("allow-pw") && !user_data.key?("ssh_pwauth")
  user_data["chpasswd"] ||= { "expire" => false }
end

File.write(output_path, "#cloud-config\n#{YAML.dump(template).sub(/\A---\n/, "")}")
