import { SignUp } from '@clerk/nextjs';
import type { Metadata } from 'next';
import { AuthLayout } from '@/components/brand/auth-layout';

export const metadata: Metadata = { title: 'Create your account' };

export default function SignUpPage() {
  return (
    <AuthLayout>
      <SignUp appearance={{ variables: { colorPrimary: '#4f46e5', borderRadius: '8px' } }} />
    </AuthLayout>
  );
}
