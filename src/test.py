s=input()
k=int(input())
l=len(s)
n=0

if l>=1 and l<=10000 and k>=1 and k<=10000:
    for i in range(0,l-k+1):
        sub=s[i:i+k]
        print(sub)
        flag=True
        for j in s:
            if sub.count(j)>1:
                flag=False
                print("False")
                break
        if flag==True:
            n+=1
print(n)
