---
title: People and roles
summary: How people, roles and apps fit together, and who can see whose work.
---

# People and roles

A person is a human account on the gateway. Azizam keeps a person separate from the apps they use and the keys those apps hold.

Two things control access:

- **Roles** control who can administer the gateway.
- **Access plans** control which requests an app can make. See [Access plans](access-plans.md).

An app key never becomes an admin key, even when the person who holds it is an administrator.

## Roles

| Role | Can administer the gateway | Can see other people's Library and Usage |
| --- | --- | --- |
| Owner | Yes | Yes |
| Superadmin | Yes | Yes |
| Admin | Yes | No |
| Member | No | No |

The gateway checks the stored role on every administration request.

Everyone starts with their own work in Library and Usage. Only an owner or a superadmin can choose another person or everyone. The list of running jobs stays personal, for the owner too.

## People and automation

A personal app belongs to a person. A service app belongs to a service account. This gives an integration its own identity, without pretending to be a person.

When you turn off a person's account, their personal apps stop working. When you turn off an app, it stops working.

Read [Apps and keys](apps-and-keys.md) and [Privacy and the owner](privacy.md) next.
